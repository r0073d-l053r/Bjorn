# comment.py
# Comments manager with database backend
# Provides contextual messages for display with timing control and multilingual support.
# comment = ai.get_comment("SSHBruteforce", params={"user": "pi", "ip": "192.168.0.12"})
# Avec un texte DB du style: "Trying {user}@{ip} over SSH..."

import os
import time
import random
import locale
from typing import Optional, List, Dict, Any

from init_shared import shared_data
from logger import Logger

logger = Logger(name="comment.py", level=20)  # INFO


# --- Helpers -----------------------------------------------------------------

class _SafeDict(dict):
    """Safe formatter: leaves unknown {placeholders} intact instead of raising."""
    def __missing__(self, key):
        return "{" + key + "}"


def _row_get(row: Any, key: str, default=None):
    """Safe accessor for rows that may be dict-like or sqlite3.Row."""
    try:
        return row.get(key, default)
    except Exception:
        try:
            return row[key]
        except Exception:
            return default


# --- Main class --------------------------------------------------------------

class CommentAI:
    """
    AI-style comment generator for status messages with:
      - Randomized delay between messages
      - Database-backed phrases (text, status, theme, lang, weight)
      - Multilingual search with language priority and fallbacks
      - Safe string templates: "Trying {user}@{ip}..."
    """

    def __init__(self):
        self.shared_data = shared_data

        # Timing configuration with robust defaults
        self.delay_min = max(1, int(getattr(self.shared_data, "comment_delaymin", 5)))
        self.delay_max = max(self.delay_min, int(getattr(self.shared_data, "comment_delaymax", 15)))
        self.comment_delay = self._new_delay()

        # State tracking
        self.last_comment_time: float = 0.0
        self.last_status: Optional[str] = None

        # Ensure comments are loaded in database
        self._ensure_comments_loaded()

        # Initialize first comment for UI using language priority
        if not hasattr(self.shared_data, "bjorn_says") or not getattr(self.shared_data, "bjorn_says"):
            first = self._pick_text("IDLE", lang=None, params=None)
            self.shared_data.bjorn_says = first or "Initializing..."

    # --- Language priority & JSON discovery ----------------------------------

    def _lang_priority(self, preferred: Optional[str] = None) -> List[str]:
        """
        Build ordered language preference list, deduplicated.
        Priority sources:
        1. explicit `preferred`
        2. shared_data.lang_priority (list)
        3. shared_data.lang (single fallback)
        4. defaults ["en", "fr"]
        """
        order: List[str] = []

        def norm(x: Optional[str]) -> Optional[str]:
            if not x:
                return None
            x = str(x).strip().lower()
            return x[:2] if x else None

        # 1) explicit override
        p = norm(preferred)
        if p:
            order.append(p)

        sd = self.shared_data

        # 2) list from shared_data
        if hasattr(sd, "lang_priority") and isinstance(sd.lang_priority, (list, tuple)):
            order += [l for l in (norm(x) for x in sd.lang_priority) if l]

        # 3) single language from shared_data
        if hasattr(sd, "lang"):
            l = norm(sd.lang)
            if l:
                order.append(l)

        # 4) fallback defaults
        order += ["en", "fr"]

        # Deduplicate while preserving order
        seen, res = set(), []
        for l in order:
            if l and l not in seen:
                seen.add(l)
                res.append(l)
        return res


    def _get_comments_json_paths(self, lang: Optional[str] = None) -> List[str]:
        """
        Return candidate JSON paths, restricted to default_comments_dir (and explicit comments_file).
        Supported patterns:
          - {comments_file} (explicit)
          - {default_comments_dir}/comments.json
          - {default_comments_dir}/comments.<lang>.json
          - {default_comments_dir}/{lang}/comments.json
        """
        lang = (lang or "").strip().lower()
        candidates = []

        # 1) Explicit path from shared_data
        comments_file = getattr(self.shared_data, "comments_file", "") or ""
        if comments_file:
            candidates.append(comments_file)

        # 2) Default comments directory
        default_dir = getattr(self.shared_data, "default_comments_dir", "")
        if default_dir:
            candidates += [
                os.path.join(default_dir, "comments.json"),
                os.path.join(default_dir, f"comments.{lang}.json") if lang else "",
                os.path.join(default_dir, lang, "comments.json") if lang else "",
            ]

        # Deduplicate
        unique_paths, seen = [], set()
        for p in candidates:
            p = (p or "").strip()
            if p and p not in seen:
                seen.add(p)
                unique_paths.append(p)

        return unique_paths


    # --- Bootstrapping DB -----------------------------------------------------

    def _ensure_comments_loaded(self):
        """Ensure comments are present in DB; import JSON if empty."""
        try:
            comment_count = int(self.shared_data.db.count_comments())
        except Exception as e:
            logger.error(f"Database error counting comments: {e}")
            comment_count = 0

        if comment_count > 0:
            logger.debug(f"Comments already in database: {comment_count}")
            return

        imported = 0
        for lang in self._lang_priority():
            for json_path in self._get_comments_json_paths(lang):
                if os.path.exists(json_path):
                    try:
                        count = int(self.shared_data.db.import_comments_from_json(json_path))
                        imported += count
                        if count > 0:
                            logger.info(f"Imported {count} comments (auto-detected lang) from {json_path}")
                            break  # stop at first successful import
                    except Exception as e:
                        logger.error(f"Failed to import comments from {json_path}: {e}")
            if imported > 0:
                break

        if imported == 0:
            logger.debug("No comments imported, seeding minimal fallback set")
            self._seed_minimal_comments()


    def _seed_minimal_comments(self):
        """
        Seed minimal set when no JSON available.
        Schema per row: (text, status, theme, lang, weight)
        """
        default_comments = [
            # English
            ("Scanning network for targets...", "NetworkScanner", "NetworkScanner", "en", 2),
            ("System idle, awaiting commands.", "IDLE", "IDLE", "en", 3),
            ("Analyzing network topology...", "NetworkScanner", "NetworkScanner", "en", 1),
            ("Processing authentication attempts...", "SSHBruteforce", "SSHBruteforce", "en", 2),
            ("Searching for vulnerabilities...", "NmapVulnScanner", "NmapVulnScanner", "en", 2),
            ("Extracting credentials from services...", "CredExtractor", "CredExtractor", "en", 1),
            ("Monitoring network changes...", "IDLE", "IDLE", "en", 2),
            ("Ready for deployment.", "IDLE", "IDLE", "en", 1),
            ("Target acquisition in progress...", "NetworkScanner", "NetworkScanner", "en", 1),
            ("Establishing secure connections...", "SSHBruteforce", "SSHBruteforce", "en", 1),

            # French (bonus minimal)
            ("Analyse du réseau en cours...", "NetworkScanner", "NetworkScanner", "fr", 2),
            ("Système au repos, en attente d’ordres.", "IDLE", "IDLE", "fr", 3),
            ("Cartographie de la topologie réseau...", "NetworkScanner", "NetworkScanner", "fr", 1),
            ("Tentatives d’authentification en cours...", "SSHBruteforce", "SSHBruteforce", "fr", 2),
            ("Recherche de vulnérabilités...", "NmapVulnScanner", "NmapVulnScanner", "fr", 2),
            ("Extraction d’identifiants depuis les services...", "CredExtractor", "CredExtractor", "fr", 1),
        ]
        try:
            self.shared_data.db.insert_comments(default_comments)
            logger.info(f"Seeded {len(default_comments)} minimal comments into database")
        except Exception as e:
            logger.error(f"Failed to seed minimal comments: {e}")

    # --- Core selection -------------------------------------------------------

    def _new_delay(self) -> int:
        """Generate new random delay between comments."""
        delay = random.randint(self.delay_min, self.delay_max)
        logger.debug(f"Next comment delay: {delay}s")
        return delay

    def _pick_text(
        self,
        status: str,
        lang: Optional[str],
        params: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """
        Pick a weighted comment across language preference; supports {templates}.
        Selection cascade (per language in priority order):
          1) (lang, status)
          2) (lang, 'ANY')
          3) (lang, 'IDLE')
        Then cross-language:
          4) (any, status)
          5) (any, 'IDLE')
        """
        status = status or "IDLE"
        langs = self._lang_priority(preferred=lang)

        # Language-scoped queries
        rows = []
        queries = [
            ("SELECT text, weight FROM comments WHERE lang=? AND status=?", lambda L: (L, status)),
            ("SELECT text, weight FROM comments WHERE lang=? AND status='ANY'", lambda L: (L,)),
            ("SELECT text, weight FROM comments WHERE lang=? AND status='IDLE'", lambda L: (L,)),
        ]
        for L in langs:
            for sql, args_fn in queries:
                try:
                    rows = self.shared_data.db.query(sql, args_fn(L))
                except Exception as e:
                    logger.error(f"DB query failed: {e}")
                    rows = []
                if rows:
                    break
            if rows:
                break

        # Cross-language fallbacks
        if not rows:
            for sql, args in [
                ("SELECT text, weight FROM comments WHERE status=? ORDER BY RANDOM() LIMIT 50", (status,)),
                ("SELECT text, weight FROM comments WHERE status='IDLE' ORDER BY RANDOM() LIMIT 50", ()),
            ]:
                try:
                    rows = self.shared_data.db.query(sql, args)
                except Exception as e:
                    logger.error(f"DB query failed: {e}")
                    rows = []
                if rows:
                    break

        if not rows:
            return None

        # Weighted selection using random.choices (no temporary list expansion)
        texts: List[str] = []
        weights: List[int] = []
        for row in rows:
            text = _row_get(row, "text", "")
            if text:
                try:
                    w = int(_row_get(row, "weight", 1)) or 1
                except Exception:
                    w = 1
                texts.append(text)
                weights.append(max(1, w))

        if texts:
            chosen = random.choices(texts, weights=weights, k=1)[0]
        else:
            chosen = _row_get(rows[0], "text", None)

        # Templates {var}
        if chosen and params:
            try:
                chosen = str(chosen).format_map(_SafeDict(params))
            except Exception:
                # Keep the raw text if formatting fails
                pass

        return chosen

    # --- Public API -----------------------------------------------------------

    def get_comment(
        self,
        status: str,
        lang: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """
        Return a comment if status changed or delay expired.

        When llm_comments_enabled=True in config, tries LLM first;
        falls back to the database/template system on any failure.

        Args:
            status: logical status name (e.g., "IDLE", "SSHBruteforce", "NetworkScanner").
            lang: language override (e.g., "fr"); if None, auto priority is used.
            params: optional dict to format templates with {placeholders}.

        Returns:
            str or None: A new comment, or None if not time yet and status unchanged.
        """
        current_time = time.time()
        status = status or "IDLE"

        status_changed = (status != self.last_status)
        if not status_changed and (current_time - self.last_comment_time < self.comment_delay):
            return None

        # --- Try LLM if enabled ---
        text: Optional[str] = None
        llm_generated = False
        if getattr(self.shared_data, "llm_comments_enabled", False):
            try:
                from llm_bridge import LLMBridge
                text = LLMBridge().generate_comment(status, params)
                if text:
                    llm_generated = True
            except Exception as e:
                logger.debug(f"LLM comment failed, using fallback: {e}")

        # --- Fallback: database / template system (original behaviour) ---
        if not text:
            text = self._pick_text(status, lang, params)

        if text:
            self.last_status = status
            self.last_comment_time = current_time
            self.comment_delay = self._new_delay()
            logger.debug(f"Next comment delay: {self.comment_delay}s")
            # Log comments
            if llm_generated:
                logger.info(f"[LLM_COMMENT] ({status}) {text}")
            else:
                logger.info(f"[COMMENT] ({status}) {text}")
            return text
        return None


# Backward compatibility alias
Commentaireia = CommentAI
