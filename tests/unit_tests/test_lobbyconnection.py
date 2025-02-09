from datetime import datetime
from unittest import mock
from unittest.mock import Mock

import pytest
from aiohttp import web
from server import GameState, VisibilityState
from server.db.models import ban, friends_and_foes
from server.game_service import GameService
from server.games import CustomGame, Game
from server.geoip_service import GeoIpService
from server.ice_servers.nts import TwilioNTS
from server.ladder_service import LadderService
from server.lobbyconnection import ClientError, LobbyConnection
from server.player_service import PlayerService
from server.players import Player, PlayerState
from server.protocol import QDataStreamProtocol
from server.rating import RatingType
from server.types import Address
from sqlalchemy import and_, select, text
from asynctest import CoroutineMock

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def test_game_info():
    return {
        'title': 'Test game',
        'visibility': VisibilityState.to_string(VisibilityState.PUBLIC),
        'mod': 'faf',
        'mapname': 'scmp_007',
        'password': None,
        'lobby_rating': 1,
        'options': []
    }


@pytest.fixture()
def test_game_info_invalid():
    return {
        'title': 'Title with non ASCI char \xc3',
        'visibility': VisibilityState.to_string(VisibilityState.PUBLIC),
        'mod': 'faf',
        'mapname': 'scmp_007',
        'password': None,
        'lobby_rating': 1,
        'options': []
    }


@pytest.fixture
def mock_player(player_factory):
    return player_factory(login='Dummy', player_id=42)


@pytest.fixture
def mock_nts_client():
    return mock.create_autospec(TwilioNTS)


@pytest.fixture
def mock_players(database):
    return mock.create_autospec(PlayerService(database))


@pytest.fixture
def mock_games(database, mock_players, game_stats_service):
    return mock.create_autospec(GameService(database, mock_players, game_stats_service))


@pytest.fixture
def mock_protocol():
    return mock.create_autospec(QDataStreamProtocol(mock.Mock(), mock.Mock()))


@pytest.fixture
def mock_geoip():
    return mock.create_autospec(GeoIpService())


@pytest.fixture
def lobbyconnection(event_loop, database, mock_protocol, mock_games, mock_players, mock_player, mock_geoip):
    lc = LobbyConnection(
        database=database,
        geoip=mock_geoip,
        games=mock_games,
        players=mock_players,
        nts_client=mock_nts_client,
        ladder_service=mock.create_autospec(LadderService)
    )

    lc.player = mock_player
    lc.protocol = mock_protocol
    lc.player_service.get_permission_group.return_value = 0
    lc.player_service.fetch_player_data = CoroutineMock()
    lc.peer_address = Address('127.0.0.1', 1234)
    return lc


@pytest.fixture
def policy_server(event_loop):
    host = 'localhost'
    port = 6080

    app = web.Application()
    routes = web.RouteTableDef()

    @routes.post('/verify')
    async def token(request):
        data = await request.json()
        return web.json_response({'result': data.get('uid_hash')})

    app.add_routes(routes)

    runner = web.AppRunner(app)

    async def start_app():
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()

    event_loop.run_until_complete(start_app())
    yield (host, port)
    event_loop.run_until_complete(runner.cleanup())


async def test_command_game_host_creates_game(lobbyconnection,
                                              mock_games,
                                              test_game_info,
                                              players):
    lobbyconnection.player = players.hosting
    lobbyconnection.protocol = mock.Mock()
    lobbyconnection.command_game_host(test_game_info)
    expected_call = {
        'game_mode': 'faf',
        'name': test_game_info['title'],
        'host': players.hosting,
        'visibility': VisibilityState.PUBLIC,
        'password': test_game_info['password'],
        'mapname': test_game_info['mapname'],
    }
    mock_games.create_game \
        .assert_called_with(**expected_call)


async def test_launch_game(lobbyconnection, game, player_factory):
    old_game_conn = mock.Mock()

    lobbyconnection.player = player_factory()
    lobbyconnection.game_connection = old_game_conn
    lobbyconnection.send = mock.Mock()
    lobbyconnection.launch_game(game)

    # Verify all side effects of launch_game here
    old_game_conn.abort.assert_called_with("Player launched a new game")
    assert lobbyconnection.game_connection is not None
    assert lobbyconnection.game_connection.game == game
    assert lobbyconnection.player.game == game
    assert lobbyconnection.player.game_connection == lobbyconnection.game_connection
    assert lobbyconnection.game_connection.player == lobbyconnection.player
    assert lobbyconnection.player.state == PlayerState.JOINING
    lobbyconnection.send.assert_called_once()


async def test_command_game_host_creates_correct_game(
        lobbyconnection, game_service, test_game_info, players):
    lobbyconnection.player = players.hosting
    lobbyconnection.game_service = game_service
    lobbyconnection.launch_game = mock.Mock()

    lobbyconnection.protocol = mock.Mock()
    lobbyconnection.command_game_host(test_game_info)
    args_list = lobbyconnection.launch_game.call_args_list
    assert len(args_list) == 1
    args, kwargs = args_list[0]
    assert isinstance(args[0], CustomGame)


async def test_command_game_join_calls_join_game(mocker,
                                                 database,
                                                 lobbyconnection,
                                                 game_service,
                                                 test_game_info,
                                                 players,
                                                 game_stats_service):
    lobbyconnection.game_service = game_service
    mock_protocol = mocker.patch.object(lobbyconnection, 'protocol')
    game = mock.create_autospec(Game(42, database, game_service, game_stats_service))
    game.state = GameState.LOBBY
    game.password = None
    game.game_mode = 'faf'
    game.id = 42
    game_service.games[42] = game
    lobbyconnection.player = players.hosting
    test_game_info['uid'] = 42

    lobbyconnection.command_game_join(test_game_info)
    expected_reply = {
        'command': 'game_launch',
        'mod': 'faf',
        'uid': 42,
        'args': ['/numgames {}'.format(players.hosting.game_count[RatingType.GLOBAL])]
    }
    mock_protocol.send_message.assert_called_with(expected_reply)


async def test_command_game_join_uid_as_str(mocker,
                                            database,
                                            lobbyconnection,
                                            game_service,
                                            test_game_info,
                                            players,
                                            game_stats_service):
    lobbyconnection.game_service = game_service
    mock_protocol = mocker.patch.object(lobbyconnection, 'protocol')
    game = mock.create_autospec(Game(42, database, game_service, game_stats_service))
    game.state = GameState.LOBBY
    game.password = None
    game.game_mode = 'faf'
    game.id = 42
    game_service.games[42] = game
    lobbyconnection.player = players.hosting
    test_game_info['uid'] = '42'  # Pass in uid as string

    lobbyconnection.command_game_join(test_game_info)
    expected_reply = {
        'command': 'game_launch',
        'mod': 'faf',
        'uid': 42,
        'args': ['/numgames {}'.format(players.hosting.game_count[RatingType.GLOBAL])]
    }
    mock_protocol.send_message.assert_called_with(expected_reply)


async def test_command_game_join_without_password(lobbyconnection,
                                                  database,
                                                  game_service,
                                                  test_game_info,
                                                  players,
                                                  game_stats_service):
    lobbyconnection.send = mock.Mock()
    lobbyconnection.game_service = game_service
    game = mock.create_autospec(Game(42, database, game_service, game_stats_service))
    game.state = GameState.LOBBY
    game.password = 'password'
    game.game_mode = 'faf'
    game.id = 42
    game_service.games[42] = game
    lobbyconnection.player = players.hosting
    test_game_info['uid'] = 42
    del test_game_info['password']

    lobbyconnection.command_game_join(test_game_info)
    lobbyconnection.send.assert_called_once_with(
        dict(command="notice", style="info", text="Bad password (it's case sensitive)"))


async def test_command_game_join_game_not_found(lobbyconnection,
                                                game_service,
                                                test_game_info,
                                                players):
    lobbyconnection.send = mock.Mock()
    lobbyconnection.game_service = game_service
    lobbyconnection.player = players.hosting
    test_game_info['uid'] = 42

    lobbyconnection.command_game_join(test_game_info)
    lobbyconnection.send.assert_called_once_with(
        dict(command="notice", style="info", text="The host has left the game"))


async def test_command_game_host_calls_host_game_invalid_title(lobbyconnection,
                                                               mock_games,
                                                               test_game_info_invalid):
    lobbyconnection.send = mock.Mock()
    mock_games.create_game = mock.Mock()
    lobbyconnection.command_game_host(test_game_info_invalid)
    assert mock_games.create_game.mock_calls == []
    lobbyconnection.send.assert_called_once_with(
        dict(command="notice", style="error", text="Non-ascii characters in game name detected."))


async def test_abort(mocker, lobbyconnection):
    proto = mocker.patch.object(lobbyconnection, 'protocol')

    lobbyconnection.abort()

    proto.writer.close.assert_any_call()


async def test_send_game_list(mocker, database, lobbyconnection, game_stats_service):
    protocol = mocker.patch.object(lobbyconnection, 'protocol')
    games = mocker.patch.object(lobbyconnection, 'game_service')  # type: GameService
    game1, game2 = mock.create_autospec(Game(42, database, mock.Mock(), game_stats_service)), \
                   mock.create_autospec(Game(22, database, mock.Mock(), game_stats_service))

    games.open_games = [game1, game2]

    lobbyconnection.send_game_list()

    protocol.send_message.assert_any_call({'command': 'game_info',
                                           'games': [game1.to_dict(), game2.to_dict()]})


async def test_send_mod_list(mocker, lobbyconnection, mock_games):
    protocol = mocker.patch.object(lobbyconnection, 'protocol')

    lobbyconnection.send_mod_list()

    protocol.send_messages.assert_called_with(mock_games.all_game_modes())


async def test_send_coop_maps(mocker, lobbyconnection):
    protocol = mocker.patch.object(lobbyconnection, 'protocol')

    await lobbyconnection.send_coop_maps()

    args = protocol.send_messages.call_args_list
    assert len(args) == 1
    coop_maps = args[0][0][0]
    for info in coop_maps:
        del info['uid']
    assert coop_maps == [
        {
            "command": "coop_info",
            "name": "FA Campaign map",
            "description": "A map from the FA campaign",
            "filename": "maps/scmp_coop_123.v0002.zip",
            "featured_mod": "coop",
            "type": "FA Campaign"
        },
        {
            "command": "coop_info",
            "name": "Aeon Campaign map",
            "description": "A map from the Aeon campaign",
            "filename": "maps/scmp_coop_124.v0000.zip",
            "featured_mod": "coop",
            "type": "Aeon Vanilla Campaign"
        },
        {
            "command": "coop_info",
            "name": "Cybran Campaign map",
            "description": "A map from the Cybran campaign",
            "filename": "maps/scmp_coop_125.v0001.zip",
            "featured_mod": "coop",
            "type": "Cybran Vanilla Campaign"
        },
        {
            "command": "coop_info",
            "name": "UEF Campaign map",
            "description": "A map from the UEF campaign",
            "filename": "maps/scmp_coop_126.v0099.zip",
            "featured_mod": "coop",
            "type": "UEF Vanilla Campaign"
        },
        {
            "command": "coop_info",
            "name": "Prothyon - 16",
            "description": "Prothyon - 16 is a secret UEF facility...",
            "filename": "maps/prothyon16.v0005.zip",
            "featured_mod": "coop",
            "type": "Custom Missions"
        }
    ]


async def test_command_admin_closelobby(mocker, lobbyconnection):
    mocker.patch.object(lobbyconnection, 'protocol')
    mocker.patch.object(lobbyconnection, '_logger')
    config = mocker.patch('server.lobbyconnection.config')
    player = mocker.patch.object(lobbyconnection, 'player')
    player.login = 'Sheeo'
    player.admin = True
    tuna = mock.Mock()
    tuna.id = 55
    lobbyconnection.player_service = {1: player, 55: tuna}

    await lobbyconnection.command_admin({
        'command': 'admin',
        'action': 'closelobby',
        'user_id': 55
    })

    tuna.lobby_connection.kick.assert_any_call(
        message=("You were kicked from FAF by an administrator (Sheeo). "
                 "Please refer to our rules for the lobby/game here {rule_link}."
                 .format(rule_link=config.RULE_LINK))
    )


async def test_command_admin_closelobby_with_ban(mocker, lobbyconnection, database):
    mocker.patch.object(lobbyconnection, 'protocol')
    config = mocker.patch('server.lobbyconnection.config')
    player = mocker.patch.object(lobbyconnection, 'player')
    player.login = 'Sheeo'
    player.id = 1
    player.admin = True
    banme = mock.Mock()
    banme.id = 200
    lobbyconnection.player_service = {1: player, banme.id: banme}
    lobbyconnection._authenticated = True

    await lobbyconnection.on_message_received({
        'command': 'admin',
        'action': 'closelobby',
        'user_id': banme.id,
        'ban': {
            'reason': 'Unit test',
            'duration': 2,
            'period': 'DAY'
        }
    })

    banme.lobby_connection.kick.assert_any_call(
        message=("You were kicked from FAF by an administrator (Sheeo). "
                 "Please refer to our rules for the lobby/game here {rule_link}."
                 .format(rule_link=config.RULE_LINK))
    )

    async with database.acquire() as conn:
        result = await conn.execute(select([ban]).where(ban.c.player_id == banme.id))

        bans = [row['reason'] async for row in result]

    assert len(bans) == 1
    assert bans[0] == 'Unit test'


async def test_command_admin_closelobby_with_ban_but_already_banned(mocker, lobbyconnection, database):
    mocker.patch.object(lobbyconnection, 'protocol')
    player = mocker.patch.object(lobbyconnection, 'player')
    player.login = 'Sheeo'
    player.id = 1
    player.admin = True
    banme = mock.Mock()
    banme.id = 200
    lobbyconnection.player_service = {1: player, banme.id: banme}
    lobbyconnection._authenticated = True

    await lobbyconnection.on_message_received({
        'command': 'admin',
        'action': 'closelobby',
        'user_id': banme.id,
        'ban': {
            'reason': 'Unit test',
            'duration': 2,
            'period': 'DAY'
        }
    })

    async with database.acquire() as conn:
        result = await conn.execute(select([ban.c.id]).where(ban.c.player_id == banme.id))
        previous_ban = await result.fetchone()

    assert previous_ban is not None

    await lobbyconnection.on_message_received({
        'command': 'admin',
        'action': 'closelobby',
        'user_id': banme.id,
        'ban': {
            'reason': 'Unit test - already banned',
            'duration': 1000
        }
    })

    async with database.acquire() as conn:
        result = await conn.execute(select([ban.c.id]).where(ban.c.player_id == banme.id))

        bans = [row['id'] async for row in result]

    assert len(bans) == 1
    assert bans[0] == previous_ban["id"]


async def test_command_admin_closelobby_with_ban_duration_no_period(mocker, lobbyconnection, database):
    mocker.patch.object(lobbyconnection, 'protocol')
    config = mocker.patch('server.lobbyconnection.config')
    player = mocker.patch.object(lobbyconnection, 'player')
    player.login = 'Sheeo'
    player.id = 1
    player.admin = True
    banme = mock.Mock()
    banme.id = 200
    lobbyconnection.player_service = {1: player, banme.id: banme}
    lobbyconnection._authenticated = True

    mocker.patch('server.lobbyconnection.func.now', return_value=text('FROM_UNIXTIME(1000)'))
    await lobbyconnection.on_message_received({
        'command': 'admin',
        'action': 'closelobby',
        'user_id': banme.id,
        'ban': {
            'reason': 'Unit test - ban duration',
            'duration': 3600*24
        }
    })

    banme.lobby_connection.kick.assert_any_call(
        message=("You were kicked from FAF by an administrator (Sheeo). "
                 "Please refer to our rules for the lobby/game here {rule_link}."
                 .format(rule_link=config.RULE_LINK))
    )

    async with database.acquire() as conn:
        result = await conn.execute(select([ban.c.expires_at]).where(ban.c.player_id == banme.id))

        bans = [row['expires_at'] async for row in result]

    assert len(bans) == 1
    assert bans[0] == datetime.utcfromtimestamp(3600*24 + 1000)


async def test_command_admin_closelobby_with_ban_bad_period(mocker, lobbyconnection, database):
    proto = mocker.patch.object(lobbyconnection, 'protocol')
    player = mocker.patch.object(lobbyconnection, 'player')
    player.admin = True
    banme = mock.Mock()
    banme.id = 1
    lobbyconnection.player_service = {1: player, banme.id: banme}
    lobbyconnection._authenticated = True

    await lobbyconnection.on_message_received({
        'command': 'admin',
        'action': 'closelobby',
        'user_id': banme.id,
        'ban': {
            'reason': 'Unit test',
            'duration': 2,
            'period': ') injected!'
        }
    })

    banme.lobbyconnection.kick.assert_not_called()
    proto.send_message.assert_called_once_with({
        'command': 'notice',
        'style': 'error',
        'text': "Period ') INJECTED!' is not allowed!"
    })

    async with database.acquire() as conn:
        result = await conn.execute(select([ban]).where(ban.c.player_id == banme.id))

        bans = [row['reason'] async for row in result]

    assert len(bans) == 0


async def test_command_admin_closelobby_with_ban_injection(mocker, lobbyconnection, database):
    proto = mocker.patch.object(lobbyconnection, 'protocol')
    player = mocker.patch.object(lobbyconnection, 'player')
    player.admin = True
    banme = mock.Mock()
    banme.id = 1
    lobbyconnection.player_service = {1: player, banme.id: banme}
    lobbyconnection._authenticated = True

    await lobbyconnection.on_message_received({
        'command': 'admin',
        'action': 'closelobby',
        'user_id': banme.id,
        'ban': {
            'reason': 'Unit test',
            'duration': 2,
            'period': ') injected!'
        }
    })

    banme.lobbyconnection.kick.assert_not_called()
    proto.send_message.assert_called_once_with({
        'command': 'notice',
        'style': 'error',
        'text': "Period ') INJECTED!' is not allowed!"
    })

    async with database.acquire() as conn:
        result = await conn.execute(select([ban]).where(ban.c.player_id == banme.id))

        bans = [row['reason'] async for row in result]

    assert len(bans) == 0


async def test_command_admin_closeFA(mocker, lobbyconnection):
    mocker.patch.object(lobbyconnection, 'protocol')
    mocker.patch.object(lobbyconnection, '_logger')
    config = mocker.patch('server.lobbyconnection.config')
    player = mocker.patch.object(lobbyconnection, 'player')
    player.login = 'Sheeo'
    player.admin = True
    player.id = 42
    tuna = mock.Mock()
    tuna.id = 55
    lobbyconnection._authenticated = True
    lobbyconnection.player_service = {42: player, 55: tuna}

    await lobbyconnection.on_message_received({
        'command': 'admin',
        'action': 'closeFA',
        'user_id': 55
    })

    tuna.lobby_connection.send.assert_any_call(dict(
        command='notice',
        style='info',
        text=("Your game was closed by an administrator (Sheeo). "
              "Please refer to our rules for the lobby/game here {rule_link}."
              .format(rule_link=config.RULE_LINK))
    ))


async def test_game_subscription(lobbyconnection: LobbyConnection):
    game = Mock()
    game.handle_action = CoroutineMock()
    lobbyconnection.game_connection = game
    lobbyconnection.ensure_authenticated = lambda _: True

    await lobbyconnection.on_message_received({'command': 'test',
                                               'args': ['foo', 42],
                                               'target': 'game'})

    game.handle_action.assert_called_with('test', ['foo', 42])


async def test_command_avatar_list(mocker, lobbyconnection: LobbyConnection, mock_player: Player):
    protocol = mocker.patch.object(lobbyconnection, 'protocol')
    lobbyconnection.player = mock_player
    lobbyconnection.player.id = 2  # Dostya test user

    await lobbyconnection.command_avatar({
        'action': 'list_avatar'
    })

    protocol.send_message.assert_any_call({
        "command": "avatar",
        "avatarlist": [{'url': 'http://content.faforever.com/faf/avatars/qai2.png', 'tooltip': 'QAI'}, {'url': 'http://content.faforever.com/faf/avatars/UEF.png', 'tooltip': 'UEF'}]
    })


async def test_command_avatar_select(mocker, database, lobbyconnection: LobbyConnection, mock_player: Player):
    lobbyconnection.player = mock_player
    lobbyconnection.player.id = 2  # Dostya test user
    lobbyconnection._authenticated = True

    await lobbyconnection.on_message_received({
        'command': 'avatar',
        'action': 'select',
        'avatar': "http://content.faforever.com/faf/avatars/qai2.png"
    })

    async with database.acquire() as conn:
        result = await conn.execute("SELECT selected from avatars where idUser=2")
        row = await result.fetchone()
        assert row[0] == 1


async def get_friends(player_id, database):
    async with database.acquire() as conn:
        result = await conn.execute(
            select([friends_and_foes.c.subject_id]).where(
                and_(
                    friends_and_foes.c.user_id == player_id,
                    friends_and_foes.c.status == 'FRIEND'
                )
            )
        )

        return [row['subject_id'] async for row in result]


async def test_command_social_add_friend(lobbyconnection, mock_player, database):
    lobbyconnection.player = mock_player
    lobbyconnection.player.id = 1
    lobbyconnection._authenticated = True

    friends = await get_friends(lobbyconnection.player.id, database)
    assert friends == []

    await lobbyconnection.on_message_received({
        'command': 'social_add',
        'friend': 2
    })

    friends = await get_friends(lobbyconnection.player.id, database)
    assert friends == [2]


async def test_command_social_remove_friend(lobbyconnection, mock_player, database):
    lobbyconnection.player = mock_player
    lobbyconnection.player.id = 2
    lobbyconnection._authenticated = True

    friends = await get_friends(lobbyconnection.player.id, database)
    assert friends == [1]

    await lobbyconnection.on_message_received({
        'command': 'social_remove',
        'friend': 1
    })

    friends = await get_friends(lobbyconnection.player.id, database)
    assert friends == []


async def test_broadcast(lobbyconnection: LobbyConnection, mocker):
    mocker.patch.object(lobbyconnection, 'protocol')
    player = mocker.patch.object(lobbyconnection, 'player')
    player.login = 'Sheeo'
    player.admin = True
    tuna = mock.Mock()
    tuna.id = 55
    lobbyconnection.player_service = [player, tuna]

    await lobbyconnection.command_admin({
        'command': 'admin',
        'action': 'broadcast',
        'message': "This is a test message"
    })

    player.lobby_connection.send_warning.assert_called_with("This is a test message")
    tuna.lobby_connection.send_warning.assert_called_with("This is a test message")


async def test_game_connection_not_restored_if_no_such_game_exists(lobbyconnection: LobbyConnection, mocker, mock_player):
    protocol = mocker.patch.object(lobbyconnection, 'protocol')
    lobbyconnection.player = mock_player
    del lobbyconnection.player.game_connection
    lobbyconnection.player.state = PlayerState.IDLE
    lobbyconnection.command_restore_game_session({'game_id': 123})

    assert not lobbyconnection.player.game_connection
    assert lobbyconnection.player.state == PlayerState.IDLE

    protocol.send_message.assert_any_call({
        "command": "notice",
        "style": "info",
        "text": "The game you were connected to does no longer exist"
    })


@pytest.mark.parametrize("game_state", [GameState.INITIALIZING, GameState.ENDED])
async def test_game_connection_not_restored_if_game_state_prohibits(lobbyconnection: LobbyConnection, game_service: GameService,
                                                                    game_stats_service, game_state, mock_player, mocker,
                                                                    database):
    protocol = mocker.patch.object(lobbyconnection, 'protocol')
    lobbyconnection.player = mock_player
    del lobbyconnection.player.game_connection
    lobbyconnection.player.state = PlayerState.IDLE
    lobbyconnection.game_service = game_service
    game = mock.create_autospec(Game(42, database, game_service, game_stats_service))
    game.state = game_state
    game.password = None
    game.game_mode = 'faf'
    game.id = 42
    game_service.games[42] = game

    lobbyconnection.command_restore_game_session({'game_id': 42})

    assert not lobbyconnection.game_connection
    assert lobbyconnection.player.state == PlayerState.IDLE

    protocol.send_message.assert_any_call({
        "command": "notice",
        "style": "info",
        "text": "The game you were connected to is no longer available"
    })


@pytest.mark.parametrize("game_state", [GameState.LIVE, GameState.LOBBY])
async def test_game_connection_restored_if_game_exists(lobbyconnection: LobbyConnection, game_service: GameService,
                                                       game_stats_service, game_state, mock_player, database):
    lobbyconnection.player = mock_player
    del lobbyconnection.player.game_connection
    lobbyconnection.player.state = PlayerState.IDLE
    lobbyconnection.game_service = game_service
    game = mock.create_autospec(Game(42, database, game_service, game_stats_service))
    game.state = game_state
    game.password = None
    game.game_mode = 'faf'
    game.id = 42
    game_service.games[42] = game

    lobbyconnection.command_restore_game_session({'game_id': 42})

    assert lobbyconnection.game_connection
    assert lobbyconnection.player.state == PlayerState.PLAYING


async def test_command_game_matchmaking(lobbyconnection, mock_player):
    lobbyconnection.player = mock_player
    lobbyconnection.player.id = 1
    lobbyconnection._authenticated = True

    await lobbyconnection.on_message_received({
        'command': 'game_matchmaking',
        'state': 'stop'
    })

    lobbyconnection.ladder_service.cancel_search.assert_called_with(lobbyconnection.player)


async def test_connection_lost(lobbyconnection):
    await lobbyconnection.on_connection_lost()

    lobbyconnection.ladder_service.on_connection_lost.assert_called_once_with(lobbyconnection.player)
    lobbyconnection.player_service.remove_player.assert_called_once_with(lobbyconnection.player)


async def test_check_policy_conformity(lobbyconnection, policy_server):
    host, port = policy_server
    with mock.patch(
        'server.lobbyconnection.FAF_POLICY_SERVER_BASE_URL',
        f'http://{host}:{port}'
    ):
        honest = await lobbyconnection.check_policy_conformity(1, "honest", session=100)
        assert honest is True


async def test_check_policy_conformity_fraudulent(lobbyconnection, policy_server, database):
    host, port = policy_server
    with mock.patch(
        'server.lobbyconnection.FAF_POLICY_SERVER_BASE_URL',
        f'http://{host}:{port}'
    ):
        # 42 is not a valid player ID which should cause a SQL constraint error
        lobbyconnection.abort = mock.Mock()
        with pytest.raises(ClientError):
            await lobbyconnection.check_policy_conformity(42, "fraudulent", session=100)

        lobbyconnection.abort = mock.Mock()
        player_id = 200
        honest = await lobbyconnection.check_policy_conformity(player_id, "fraudulent", session=100)
        assert honest is False
        lobbyconnection.abort.assert_called_once()

        # Check that the user has a ban entry in the database
        async with database.acquire() as conn:
            result = await conn.execute(select([ban.c.reason]).where(
                ban.c.player_id == player_id
            ))
            rows = await result.fetchall()
            assert rows is not None
            assert rows[-1][ban.c.reason] == "Auto-banned because of fraudulent login attempt"


async def test_check_policy_conformity_fatal(lobbyconnection, policy_server):
    host, port = policy_server
    with mock.patch(
        'server.lobbyconnection.FAF_POLICY_SERVER_BASE_URL',
        f'http://{host}:{port}'
    ):
        for result in ('vm', 'already_associated', 'fraudulent'):
            lobbyconnection.abort = mock.Mock()
            honest = await lobbyconnection.check_policy_conformity(1, result, session=100)
            assert honest is False
            lobbyconnection.abort.assert_called_once()
