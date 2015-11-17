import weakref

from enum import IntEnum, unique

from .abc.base_player import BasePlayer

@unique
class PlayerState(IntEnum):
    IDLE = 1,
    PLAYING = 2,
    HOSTING = 3,
    JOINING = 4,
    SEARCHING_LADDER = 5,

class Player(BasePlayer):
    """
    Standard player object used for representing signed-in players.

    In the context of a game, the Game object holds game-specific
    information about players.
    """
    def __init__(self, login=None, session=0, ip=None, port=None, id=0,
                 global_rating=(1500, 500), ladder_rating=(1500, 500), clan=None, numGames=0, permissionGroup=0, lobby_connection=None):
        super().__init__(id, login)

        # The id of the user in the `login` table of the database.
        self.session = session
        self.ip = ip
        self._game_port = port

        self.global_rating = global_rating
        self.ladder_rating = ladder_rating

        #social
        self.avatar = None
        self.clan = clan
        self.country = None

        self.friends = set()
        self.foes = set()

        self.league = None

        self.admin = permissionGroup >= 2
        self.mod = permissionGroup >= 1
        
        self.numGames = numGames

        self.state = PlayerState.IDLE

        self.expandLadder = 0
        self.faction = 1

        self._lobby_connection = lambda: None
        if lobbyThread is not None:
            self.lobby_connection = lobbyThread

        self._game = lambda: None
        self._game_connection = lambda: None

    def getAddress(self):
        return "%s:%s" % (str(self.ip), str(self.game_port))


    @property
    def lobby_connection(self) -> "LobbyConnection":
        """
        Weak reference to the LobbyConnection of this player
        """
        return self._lobby_connection()

    @lobby_connection.setter
    def lobby_connection(self, value: "LobbyConnection"):
        self._lobby_connection = weakref.ref(value)

    @property
    def game(self):
        """
        Weak reference to the Game object that this player wants to join or is currently in
        """
        return self._game()

    @game.setter
    def game(self, value):
        self._game = weakref.ref(value)

    @game.deleter
    def game(self):
        self._game = lambda: None

    @property
    def game_connection(self):
        """
        Weak reference to the GameConnection object for this player
        :return:
        """
        return self._game_connection()

    @game_connection.setter
    def game_connection(self, value):
        self._game_connection = weakref.ref(value)

    @game_connection.deleter
    def game_connection(self):
        self._game_connection = lambda: None

    @property
    def in_game(self):
        return self.game is not None

    @property
    def game_port(self):
        return self._game_port or 6112

    @game_port.setter
    def game_port(self, value):
        self._game_port = value

    @property
    def address_and_port(self):
        return "{}:{}".format(self.ip, self.game_port)

    def notify_potential_match(self, player, potential):
        if self.lobby_connection:
            self.lobby_connection.sendJSON({
                'command': 'matchmaker_info',
                'potential': potential
            })

    def to_dict(self):
        """
        Return a dictionary representing this player object
        :return:
        """
        def filter_none(t):
            _, v = t
            return v is not None
        return dict(filter(filter_none, (
            ('id', self.id),
            ('login', self.login),
            ('global_rating', self.global_rating),
            ('ladder_rating', self.ladder_rating),
            ('number_of_games', self.numGames),
            ('avatar', self.avatar),
            ('country', self.country),
            ('clan', self.clan)
        )))

    def __str__(self):
        return "Player({}, {}, {}, {})".format(self.login, self.id, self.global_rating, self.ladder_rating)

    def __repr__(self):
        return self.__str__()

    def __hash__(self):
        return self.id

    def __eq__(self, other):
        if not isinstance(other, BasePlayer):
            return False
        else:
            return self.id == other.id
