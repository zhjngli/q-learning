from abc import ABC
from collections import deque
from random import shuffle
from typing import Callable, Deque, Generic, List, NamedTuple, Optional, Tuple

import numpy as np

from games.game import (
    P1,
    P1WIN,
    P2WIN,
    Action,
    Board,
    Game,
    ImmutableRepresentation,
    Player,
    State,
)
from learners.alpha_zero.monte_carlo_tree_search import (
    MCTSParameters,
    MonteCarloTreeSearch,
)
from nn.neural_network import NeuralNetwork, Policy


class A0Parameters(NamedTuple):
    temp_threshold: int
    pit_games: int
    pit_threshold: float
    training_episodes: int
    training_games_per_episode: int
    training_queue_length: int
    training_hist_max_len: int


class AlphaZero(ABC, Generic[State, ImmutableRepresentation]):
    """
    Combines a neural network with Monte Carlo Tree Search to increase training efficiency and reduce memory required for training.
    """

    def __init__(
        self,
        game: Game[State, ImmutableRepresentation],
        nn: NeuralNetwork,
        params: A0Parameters,
        m_params: MCTSParameters,
    ) -> None:
        self.game = game
        self.nn = nn  # current neural network
        self.pn = nn  # previous neural network for self-play. TODO: some other form of previous?
        self.m = MonteCarloTreeSearch(self.game, self.nn, m_params)
        self.training_history: List[Deque[Tuple[Board, Policy, float]]] = []

        self.m_params = m_params

        self.temperature_threshold = params.temp_threshold
        self.pit_games = params.pit_games
        self.pit_threshold = params.pit_threshold
        self.training_episodes = params.training_episodes
        self.training_games_per_episode = params.training_games_per_episode
        self.training_queue_length = params.training_queue_length
        self.training_hist_max_len = params.training_hist_max_len

    def train_once(self) -> List[Tuple[Board, Policy, float]]:
        self.game.reset()

        training_data: List[Tuple[Board, Player, Policy, Optional[float]]] = []
        state = self.game.state()
        player = state.player

        turn = 0
        while not self.game.finished(state):
            turn += 1
            oriented_state = self.game.oriented_state(state)
            temperature = 1 if turn < self.temperature_threshold else 0
            pi = self.m.action_probabilities(oriented_state, temperature)
            bs = self.game.symmetries(oriented_state.board)
            pis = self.game.symmetries(np.asarray(pi))

            for b, p in zip(bs, pis):
                training_data.append((b, player, p, None))

            action = np.random.choice(len(pi), p=pi)

            state = self.game.apply(state, action)
            player = state.player

        reward = self.game.reward(state)
        return [
            (x[0], x[2], reward * ((-1) ** (x[1] != player))) for x in training_data
        ]

    def train(self) -> None:
        for i in range(1, self.training_episodes + 1):
            # self play
            self_play_data: Deque[Tuple[Board, Policy, float]] = deque(
                [], maxlen=self.training_queue_length
            )
            for _ in range(self.training_games_per_episode):
                self.m = MonteCarloTreeSearch(self.game, self.nn, self.m_params)
                self_play_data.extend(self.train_once())

            self.training_history.append(self_play_data)

            if len(self.training_history) > self.training_hist_max_len:
                self.training_history.pop(0)

            # train model
            self.nn.save("temp_model.h5")
            self.pn.load("temp_model.h5")

            training_data = [
                d for game_data in self.training_history for d in game_data
            ]
            shuffle(training_data)
            self.nn.train(training_data)

            # if model is good enough, keep it
            if self.pit():
                self.nn.save(f"ep_{i:07d}_model.h5")
                self.nn.save("best_model.h5")
            else:
                self.nn.load("temp_model.h5")

    def pit(self) -> bool:
        prev_mtcs = MonteCarloTreeSearch(self.game, self.pn, self.m_params)
        candidate = MonteCarloTreeSearch(self.game, self.nn, self.m_params)
        play1: Callable[[State], Action] = lambda s: int(
            np.argmax(prev_mtcs.action_probabilities(s, temperature=0))
        )
        play2: Callable[[State], Action] = lambda s: int(
            np.argmax(candidate.action_probabilities(s, temperature=0))
        )

        # TODO: what's the effect of using the neural network's prediction instead of tree search?
        # play1 = lambda s: np.argmax(self.pn.predict(s)[0])
        # play2 = lambda s: np.argmax(self.nn.predict(s)[0])

        p1wins = 0
        p2wins = 0
        draws = 0
        for i in range(self.pit_games):
            if i == int(self.pit_games / 2):
                # switch first player
                play1, play2 = play2, play1
                p1wins, p2wins = p2wins, p1wins

            self.game.reset()
            state = self.game.state()
            player = state.player

            while not self.game.finished(state):
                # oriented_state = self.game.oriented_state(state)  # only needed for nn prediction
                play = play1 if player == P1 else play2
                a = play(state)
                state = self.game.apply(state, a)
                player = state.player

            r = self.game.reward(state)
            if r == P1WIN:
                p1wins += 1
            elif r == P2WIN:
                p2wins += 1
            else:
                draws += 1

        # TODO: should win percentage be based on total games?
        # candidate becomes p1 after the switch
        return p1wins + p2wins != 0 and p1wins / (p1wins + p2wins) > self.pit_threshold
