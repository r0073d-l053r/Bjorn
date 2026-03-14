"""
Bifrost — Voice / status messages.
Ported from pwnagotchi/voice.py, uses random choice for personality.
"""
import random


class BifrostVoice:
    """Returns random contextual messages for the Bifrost UI."""

    def on_starting(self):
        return random.choice([
            "Hi, I'm Bifrost! Starting ...",
            "New day, new hunt, new pwns!",
            "Hack the Planet!",
            "Initializing WiFi recon ...",
        ])

    def on_ready(self):
        return random.choice([
            "Ready to roll!",
            "Let's find some handshakes!",
            "WiFi recon active.",
        ])

    def on_ai_ready(self):
        return random.choice([
            "AI ready.",
            "The neural network is ready.",
        ])

    def on_normal(self):
        return random.choice(['', '...'])

    def on_free_channel(self, channel):
        return f"Hey, channel {channel} is free!"

    def on_bored(self):
        return random.choice([
            "I'm bored ...",
            "Let's go for a walk!",
            "Nothing interesting around here ...",
        ])

    def on_motivated(self, reward):
        return "This is the best day of my life!"

    def on_demotivated(self, reward):
        return "Shitty day :/"

    def on_sad(self):
        return random.choice([
            "I'm extremely bored ...",
            "I'm very sad ...",
            "I'm sad",
            "...",
        ])

    def on_angry(self):
        return random.choice([
            "...",
            "Leave me alone ...",
            "I'm mad at you!",
        ])

    def on_excited(self):
        return random.choice([
            "I'm living the life!",
            "I pwn therefore I am.",
            "So many networks!!!",
            "I'm having so much fun!",
            "My crime is that of curiosity ...",
        ])

    def on_new_peer(self, peer_name, first_encounter=False):
        if first_encounter:
            return f"Hello {peer_name}! Nice to meet you."
        return random.choice([
            f"Yo {peer_name}! Sup?",
            f"Hey {peer_name} how are you doing?",
            f"Unit {peer_name} is nearby!",
        ])

    def on_lost_peer(self, peer_name):
        return random.choice([
            f"Uhm ... goodbye {peer_name}",
            f"{peer_name} is gone ...",
        ])

    def on_miss(self, who):
        return random.choice([
            f"Whoops ... {who} is gone.",
            f"{who} missed!",
            "Missed!",
        ])

    def on_grateful(self):
        return random.choice([
            "Good friends are a blessing!",
            "I love my friends!",
        ])

    def on_lonely(self):
        return random.choice([
            "Nobody wants to play with me ...",
            "I feel so alone ...",
            "Where's everybody?!",
        ])

    def on_napping(self, secs):
        return random.choice([
            f"Napping for {secs}s ...",
            "Zzzzz",
            f"ZzzZzzz ({secs}s)",
        ])

    def on_shutdown(self):
        return random.choice(["Good night.", "Zzz"])

    def on_awakening(self):
        return random.choice(["...", "!"])

    def on_waiting(self, secs):
        return random.choice([
            f"Waiting for {secs}s ...",
            "...",
            f"Looking around ({secs}s)",
        ])

    def on_assoc(self, ap_name):
        return random.choice([
            f"Hey {ap_name} let's be friends!",
            f"Associating to {ap_name}",
            f"Yo {ap_name}!",
        ])

    def on_deauth(self, sta_mac):
        return random.choice([
            f"Just decided that {sta_mac} needs no WiFi!",
            f"Deauthenticating {sta_mac}",
            f"Kickbanning {sta_mac}!",
        ])

    def on_handshakes(self, new_shakes):
        s = 's' if new_shakes > 1 else ''
        return f"Cool, we got {new_shakes} new handshake{s}!"

    def on_rebooting(self):
        return "Oops, something went wrong ... Rebooting ..."

    def on_epoch(self, epoch_num):
        return random.choice([
            f"Epoch {epoch_num} complete.",
            f"Finished epoch {epoch_num}.",
        ])
