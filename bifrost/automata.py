"""automata.py - Bifrost mood state machine.

Ported from pwnagotchi/automata.py.
"""
import logging

from bifrost import plugins as plugins
from bifrost.faces import MOOD_FACES

from logger import Logger

logger = Logger(name="bifrost.automata", level=logging.DEBUG)


class BifrostAutomata:
    """Evaluates epoch data and transitions between moods."""

    def __init__(self, config):
        self._config = config
        self.mood = 'starting'
        self.face = MOOD_FACES.get('starting', '(. .)')
        self.voice_text = ''
        self._peers = {}  # peer_id -> peer_data

    @property
    def peers(self):
        return self._peers

    def _set_mood(self, mood):
        self.mood = mood
        self.face = MOOD_FACES.get(mood, '(. .)')

    def set_starting(self):
        self._set_mood('starting')

    def set_ready(self):
        self._set_mood('ready')
        plugins.on('ready')

    def _has_support_network_for(self, factor):
        bond_factor = self._config.get('bifrost_personality_bond_factor', 20000)
        total_encounters = sum(
            p.get('encounters', 0) if isinstance(p, dict) else getattr(p, 'encounters', 0)
            for p in self._peers.values()
        )
        support_factor = total_encounters / bond_factor
        return support_factor >= factor

    def in_good_mood(self):
        return self._has_support_network_for(1.0)

    def set_grateful(self):
        self._set_mood('grateful')
        plugins.on('grateful')

    def set_lonely(self):
        if not self._has_support_network_for(1.0):
            logger.info("unit is lonely")
            self._set_mood('lonely')
            plugins.on('lonely')
        else:
            logger.info("unit is grateful instead of lonely")
            self.set_grateful()

    def set_bored(self, inactive_for):
        bored_epochs = self._config.get('bifrost_personality_bored_epochs', 15)
        factor = inactive_for / bored_epochs if bored_epochs else 1
        if not self._has_support_network_for(factor):
            logger.warning("%d epochs with no activity -> bored", inactive_for)
            self._set_mood('bored')
            plugins.on('bored')
        else:
            logger.info("unit is grateful instead of bored")
            self.set_grateful()

    def set_sad(self, inactive_for):
        sad_epochs = self._config.get('bifrost_personality_sad_epochs', 25)
        factor = inactive_for / sad_epochs if sad_epochs else 1
        if not self._has_support_network_for(factor):
            logger.warning("%d epochs with no activity -> sad", inactive_for)
            self._set_mood('sad')
            plugins.on('sad')
        else:
            logger.info("unit is grateful instead of sad")
            self.set_grateful()

    def set_angry(self, factor):
        if not self._has_support_network_for(factor):
            logger.warning("too many misses -> angry (factor=%.1f)", factor)
            self._set_mood('angry')
            plugins.on('angry')
        else:
            logger.info("unit is grateful instead of angry")
            self.set_grateful()

    def set_excited(self):
        logger.warning("lots of activity -> excited")
        self._set_mood('excited')
        plugins.on('excited')

    def set_rebooting(self):
        self._set_mood('broken')
        plugins.on('rebooting')

    def next_epoch(self, epoch):
        """Evaluate epoch state and transition mood.

        Args:
            epoch: BifrostEpoch instance
        """
        was_stale = epoch.num_missed > self._config.get('bifrost_personality_max_misses', 8)
        did_miss = epoch.num_missed

        # Trigger epoch transition (resets counters, computes reward)
        epoch.next()

        max_misses = self._config.get('bifrost_personality_max_misses', 8)
        excited_threshold = self._config.get('bifrost_personality_excited_epochs', 10)

        # Mood evaluation (same logic as pwnagotchi automata.py)
        if was_stale:
            factor = did_miss / max_misses if max_misses else 1
            if factor >= 2.0:
                self.set_angry(factor)
            else:
                logger.warning("agent missed %d interactions -> lonely", did_miss)
                self.set_lonely()
        elif epoch.sad_for:
            sad_epochs = self._config.get('bifrost_personality_sad_epochs', 25)
            factor = epoch.inactive_for / sad_epochs if sad_epochs else 1
            if factor >= 2.0:
                self.set_angry(factor)
            else:
                self.set_sad(epoch.inactive_for)
        elif epoch.bored_for:
            self.set_bored(epoch.inactive_for)
        elif epoch.active_for >= excited_threshold:
            self.set_excited()
        elif epoch.active_for >= 5 and self._has_support_network_for(5.0):
            self.set_grateful()

        plugins.on('epoch', epoch.epoch - 1, epoch.data())

    def on_miss(self, who):
        logger.info("it looks like %s is not in range anymore :/", who)

    def on_error(self, who, e):
        if 'is an unknown BSSID' in str(e):
            self.on_miss(who)
        else:
            logger.error(str(e))

    def is_stale(self, epoch):
        return epoch.num_missed > self._config.get('bifrost_personality_max_misses', 8)

    def wait_for(self, t, epoch, sleeping=True, stop_event=None):
        """Wait and track sleep time.

        If *stop_event* is provided the wait is interruptible so the
        engine can shut down quickly even during long recon windows.
        """
        plugins.on('sleep' if sleeping else 'wait', t)
        epoch.track(sleep=True, inc=t)
        import time
        if stop_event is not None:
            stop_event.wait(t)
        else:
            time.sleep(t)
