"""bifrost.py - Networks, handshakes, epochs, activity, peers, plugin data."""
import logging

from logger import Logger

logger = Logger(name="db_utils.bifrost", level=logging.DEBUG)


class BifrostOps:
    def __init__(self, base):
        self.base = base

    def create_tables(self):
        """Create all Bifrost tables."""

        # WiFi networks discovered by Bifrost
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS bifrost_networks (
                bssid         TEXT PRIMARY KEY,
                essid         TEXT DEFAULT '',
                channel       INTEGER DEFAULT 0,
                encryption    TEXT DEFAULT '',
                rssi          INTEGER DEFAULT 0,
                vendor        TEXT DEFAULT '',
                num_clients   INTEGER DEFAULT 0,
                first_seen    TEXT DEFAULT CURRENT_TIMESTAMP,
                last_seen     TEXT DEFAULT CURRENT_TIMESTAMP,
                handshake     INTEGER DEFAULT 0,
                deauthed      INTEGER DEFAULT 0,
                associated    INTEGER DEFAULT 0,
                whitelisted   INTEGER DEFAULT 0
            )
        """)

        # Captured handshakes
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS bifrost_handshakes (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ap_mac        TEXT NOT NULL,
                sta_mac       TEXT NOT NULL,
                ap_essid      TEXT DEFAULT '',
                channel       INTEGER DEFAULT 0,
                rssi          INTEGER DEFAULT 0,
                filename      TEXT DEFAULT '',
                captured_at   TEXT DEFAULT CURRENT_TIMESTAMP,
                uploaded      INTEGER DEFAULT 0,
                cracked       INTEGER DEFAULT 0,
                UNIQUE(ap_mac, sta_mac)
            )
        """)

        # Epoch history
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS bifrost_epochs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                epoch_num       INTEGER NOT NULL,
                started_at      TEXT NOT NULL,
                duration_secs   REAL DEFAULT 0,
                num_deauths     INTEGER DEFAULT 0,
                num_assocs      INTEGER DEFAULT 0,
                num_handshakes  INTEGER DEFAULT 0,
                num_hops        INTEGER DEFAULT 0,
                num_missed      INTEGER DEFAULT 0,
                num_peers       INTEGER DEFAULT 0,
                mood            TEXT DEFAULT 'ready',
                reward          REAL DEFAULT 0,
                cpu_load        REAL DEFAULT 0,
                mem_usage       REAL DEFAULT 0,
                temperature     REAL DEFAULT 0,
                meta_json       TEXT DEFAULT '{}'
            )
        """)

        # Activity log (event feed)
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS bifrost_activity (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp     TEXT DEFAULT CURRENT_TIMESTAMP,
                event_type    TEXT NOT NULL,
                title         TEXT NOT NULL,
                details       TEXT DEFAULT '',
                meta_json     TEXT DEFAULT '{}'
            )
        """)
        self.base.execute(
            "CREATE INDEX IF NOT EXISTS idx_bifrost_activity_ts "
            "ON bifrost_activity(timestamp DESC)"
        )

        # Peers (mesh networking - Phase 2)
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS bifrost_peers (
                peer_id       TEXT PRIMARY KEY,
                name          TEXT DEFAULT '',
                version       TEXT DEFAULT '',
                face          TEXT DEFAULT '',
                encounters    INTEGER DEFAULT 0,
                last_channel  INTEGER DEFAULT 0,
                last_seen     TEXT DEFAULT CURRENT_TIMESTAMP,
                first_seen    TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Plugin persistent state
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS bifrost_plugin_data (
                plugin_name   TEXT NOT NULL,
                key           TEXT NOT NULL,
                value         TEXT DEFAULT '',
                PRIMARY KEY (plugin_name, key)
            )
        """)

        logger.debug("Bifrost tables created/verified")
