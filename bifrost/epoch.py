"""epoch.py - Bifrost epoch tracking and reward signals.

Ported from pwnagotchi/ai/epoch.py + pwnagotchi/ai/reward.py.
"""
import time
import threading
import logging
import os

from logger import Logger

logger = Logger(name="bifrost.epoch", level=logging.DEBUG)

NUM_CHANNELS = 14  # 2.4 GHz channels


# ── Reward function (from pwnagotchi/ai/reward.py) ──────────────

class RewardFunction:
    """Reward signal for RL - higher is better."""

    def __call__(self, epoch_n, state):
        eps = 1e-20
        tot_epochs = epoch_n + eps
        tot_interactions = max(
            state['num_deauths'] + state['num_associations'],
            state['num_handshakes']
        ) + eps
        tot_channels = NUM_CHANNELS

        # Positive signals
        h = state['num_handshakes'] / tot_interactions
        a = 0.2 * (state['active_for_epochs'] / tot_epochs)
        c = 0.1 * (state['num_hops'] / tot_channels)

        # Negative signals
        b = -0.3 * (state['blind_for_epochs'] / tot_epochs)
        m = -0.3 * (state['missed_interactions'] / tot_interactions)
        i = -0.2 * (state['inactive_for_epochs'] / tot_epochs)

        _sad = state['sad_for_epochs'] if state['sad_for_epochs'] >= 5 else 0
        _bored = state['bored_for_epochs'] if state['bored_for_epochs'] >= 5 else 0
        s = -0.2 * (_sad / tot_epochs)
        l_val = -0.1 * (_bored / tot_epochs)

        return h + a + c + b + i + m + s + l_val


# ── Epoch state ──────────────────────────────────────────────────

class BifrostEpoch:
    """Tracks per-epoch counters, observations, and reward."""

    def __init__(self, config):
        self.epoch = 0
        self.config = config

        # Consecutive epoch counters
        self.inactive_for = 0
        self.active_for = 0
        self.blind_for = 0
        self.sad_for = 0
        self.bored_for = 0

        # Per-epoch action flags & counters
        self.did_deauth = False
        self.num_deauths = 0
        self.did_associate = False
        self.num_assocs = 0
        self.num_missed = 0
        self.did_handshakes = False
        self.num_shakes = 0
        self.num_hops = 0
        self.num_slept = 0
        self.num_peers = 0
        self.tot_bond_factor = 0.0
        self.avg_bond_factor = 0.0
        self.any_activity = False

        # Timing
        self.epoch_started = time.time()
        self.epoch_duration = 0

        # Channel histograms for AI observation
        self.non_overlapping_channels = {1: 0, 6: 0, 11: 0}
        self._observation = {
            'aps_histogram': [0.0] * NUM_CHANNELS,
            'sta_histogram': [0.0] * NUM_CHANNELS,
            'peers_histogram': [0.0] * NUM_CHANNELS,
        }
        self._observation_ready = threading.Event()
        self._epoch_data = {}
        self._epoch_data_ready = threading.Event()
        self._reward = RewardFunction()

    def wait_for_epoch_data(self, with_observation=True, timeout=None):
        self._epoch_data_ready.wait(timeout)
        self._epoch_data_ready.clear()
        if with_observation:
            return {**self._observation, **self._epoch_data}
        return self._epoch_data

    def data(self):
        return self._epoch_data

    def observe(self, aps, peers):
        """Update observation histograms from current AP/peer lists."""
        num_aps = len(aps)
        if num_aps == 0:
            self.blind_for += 1
        else:
            self.blind_for = 0

        bond_unit_scale = self.config.get('bifrost_personality_bond_factor', 20000)
        self.num_peers = len(peers)
        num_peers = self.num_peers + 1e-10

        self.tot_bond_factor = sum(
            p.get('encounters', 0) if isinstance(p, dict) else getattr(p, 'encounters', 0)
            for p in peers
        ) / bond_unit_scale
        self.avg_bond_factor = self.tot_bond_factor / num_peers

        num_aps_f = len(aps) + 1e-10
        num_sta = sum(len(ap.get('clients', [])) for ap in aps) + 1e-10
        aps_per_chan = [0.0] * NUM_CHANNELS
        sta_per_chan = [0.0] * NUM_CHANNELS
        peers_per_chan = [0.0] * NUM_CHANNELS

        for ap in aps:
            ch_idx = ap.get('channel', 1) - 1
            if 0 <= ch_idx < NUM_CHANNELS:
                aps_per_chan[ch_idx] += 1.0
                sta_per_chan[ch_idx] += len(ap.get('clients', []))

        for peer in peers:
            ch = peer.get('last_channel', 0) if isinstance(peer, dict) else getattr(peer, 'last_channel', 0)
            ch_idx = ch - 1
            if 0 <= ch_idx < NUM_CHANNELS:
                peers_per_chan[ch_idx] += 1.0

        # Normalize
        aps_per_chan = [e / num_aps_f for e in aps_per_chan]
        sta_per_chan = [e / num_sta for e in sta_per_chan]
        peers_per_chan = [e / num_peers for e in peers_per_chan]

        self._observation = {
            'aps_histogram': aps_per_chan,
            'sta_histogram': sta_per_chan,
            'peers_histogram': peers_per_chan,
        }
        self._observation_ready.set()

    def track(self, deauth=False, assoc=False, handshake=False,
              hop=False, sleep=False, miss=False, inc=1):
        """Increment epoch counters."""
        if deauth:
            self.num_deauths += inc
            self.did_deauth = True
            self.any_activity = True

        if assoc:
            self.num_assocs += inc
            self.did_associate = True
            self.any_activity = True

        if miss:
            self.num_missed += inc

        if hop:
            self.num_hops += inc
            # Reset per-channel flags on hop
            self.did_deauth = False
            self.did_associate = False

        if handshake:
            self.num_shakes += inc
            self.did_handshakes = True

        if sleep:
            self.num_slept += inc

    def next(self):
        """Transition to next epoch - compute reward, update streaks, reset counters."""
        # Update activity streaks
        if not self.any_activity and not self.did_handshakes:
            self.inactive_for += 1
            self.active_for = 0
        else:
            self.active_for += 1
            self.inactive_for = 0
            self.sad_for = 0
            self.bored_for = 0

        sad_threshold = self.config.get('bifrost_personality_sad_epochs', 25)
        bored_threshold = self.config.get('bifrost_personality_bored_epochs', 15)

        if self.inactive_for >= sad_threshold:
            self.bored_for = 0
            self.sad_for += 1
        elif self.inactive_for >= bored_threshold:
            self.sad_for = 0
            self.bored_for += 1
        else:
            self.sad_for = 0
            self.bored_for = 0

        now = time.time()
        self.epoch_duration = now - self.epoch_started

        # System metrics
        cpu = _cpu_load()
        mem = _mem_usage()
        temp = _temperature()

        # Cache epoch data for other threads
        self._epoch_data = {
            'duration_secs': self.epoch_duration,
            'slept_for_secs': self.num_slept,
            'blind_for_epochs': self.blind_for,
            'inactive_for_epochs': self.inactive_for,
            'active_for_epochs': self.active_for,
            'sad_for_epochs': self.sad_for,
            'bored_for_epochs': self.bored_for,
            'missed_interactions': self.num_missed,
            'num_hops': self.num_hops,
            'num_peers': self.num_peers,
            'tot_bond': self.tot_bond_factor,
            'avg_bond': self.avg_bond_factor,
            'num_deauths': self.num_deauths,
            'num_associations': self.num_assocs,
            'num_handshakes': self.num_shakes,
            'cpu_load': cpu,
            'mem_usage': mem,
            'temperature': temp,
        }
        self._epoch_data['reward'] = self._reward(self.epoch + 1, self._epoch_data)
        self._epoch_data_ready.set()

        logger.info(
            "[epoch %d] dur=%ds blind=%d sad=%d bored=%d inactive=%d active=%d "
            "hops=%d missed=%d deauths=%d assocs=%d shakes=%d reward=%.3f",
            self.epoch, int(self.epoch_duration), self.blind_for,
            self.sad_for, self.bored_for, self.inactive_for, self.active_for,
            self.num_hops, self.num_missed, self.num_deauths, self.num_assocs,
            self.num_shakes, self._epoch_data['reward'],
        )

        # Reset for next epoch
        self.epoch += 1
        self.epoch_started = now
        self.did_deauth = False
        self.num_deauths = 0
        self.num_peers = 0
        self.tot_bond_factor = 0.0
        self.avg_bond_factor = 0.0
        self.did_associate = False
        self.num_assocs = 0
        self.num_missed = 0
        self.did_handshakes = False
        self.num_shakes = 0
        self.num_hops = 0
        self.num_slept = 0
        self.any_activity = False


# ── System metric helpers ────────────────────────────────────────

def _cpu_load():
    try:
        return os.getloadavg()[0]
    except (OSError, AttributeError):
        return 0.0


def _mem_usage():
    try:
        with open('/proc/meminfo', 'r') as f:
            lines = f.readlines()
        total = int(lines[0].split()[1])
        available = int(lines[2].split()[1])
        return (total - available) / total if total else 0.0
    except Exception:
        return 0.0


def _temperature():
    try:
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            return int(f.read().strip()) / 1000.0
    except Exception:
        return 0.0
