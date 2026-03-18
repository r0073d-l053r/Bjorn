"""comment_utils.py - Status comments and messages displayed in the UI."""
from __future__ import annotations
import json
import re
import traceback
from typing import Any, Dict, Optional

import logging
from logger import Logger
logger = Logger(name="comment_utils.py", level=logging.DEBUG)
class CommentUtils:
    """Utilities for managing comments and status messages."""

    def __init__(self, shared_data):
        self.logger = logger
        self.shared_data = shared_data

    def get_sections(self, handler):
        """Get list of comment sections (statuses) from DB."""
        try:
            rows = self.shared_data.db.query("SELECT DISTINCT status FROM comments ORDER BY status;")
            sections = [r["status"] for r in rows]

            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            response = json.dumps({'status': 'success', 'sections': sections})
            handler.wfile.write(response.encode('utf-8'))
        except Exception as e:
            self.logger.error(f"Error in get_sections: {e}")
            handler.send_response(500)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            error_response = json.dumps({'status': 'error', 'message': str(e)})
            handler.wfile.write(error_response.encode('utf-8'))

    def get_comments(self, handler):
        """Get comments for a specific section from DB."""
        try:
            from urllib.parse import urlparse, parse_qs
            query_components = parse_qs(urlparse(handler.path).query)
            section = query_components.get('section', [None])[0]
            if not section:
                raise ValueError('Section parameter is required')

            rows = self.shared_data.db.query(
                "SELECT text FROM comments WHERE status=? ORDER BY id;",
                (section,)
            )
            comments = [r["text"] for r in rows]

            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            response = json.dumps({'status': 'success', 'comments': comments})
            handler.wfile.write(response.encode('utf-8'))
        except Exception as e:
            self.logger.error(f"Error in get_comments: {e}")
            handler.send_response(500)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            error_response = json.dumps({'status': 'error', 'message': str(e)})
            handler.wfile.write(error_response.encode('utf-8'))

    def save_comments(self, data):
        """Save comment list for a section to DB (replaces existing)."""
        try:
            section = data.get('section')
            comments = data.get('comments')
            lang = data.get('lang', 'fr')
            theme = data.get('theme', section or 'general')
            weight = int(data.get('weight', 1))

            if not section or comments is None:
                return {'status': 'error', 'message': 'Section and comments are required'}

            if not isinstance(comments, list):
                return {'status': 'error', 'message': 'Comments must be a list of strings'}

            # Replace section content
            with self.shared_data.db.transaction(immediate=True):
                self.shared_data.db.execute("DELETE FROM comments WHERE status=? AND lang=?", (section, lang))
                rows = []
                for txt in comments:
                    t = str(txt).strip()
                    if not t:
                        continue
                    rows.append((t, section, theme, lang, weight))
                if rows:
                    self.shared_data.db.insert_comments(rows)

            return {'status': 'success', 'message': 'Comments saved successfully'}
        except Exception as e:
            self.logger.error(f"Error in save_comments: {e}")
            return {'status': 'error', 'message': str(e)}

    def restore_default_comments(self, data=None):
        """Restore default comments from JSON file to DB."""
        try:
            inserted = self.shared_data.db.import_comments_from_json(
                self.shared_data.default_comments_file,
                lang=(data.get('lang') if isinstance(data, dict) else None) or 'fr',
                clear_existing=True
            )
            return {
                'status': 'success',
                'message': f'Comments restored ({inserted} entries).'
            }
        except Exception as e:
            self.logger.error(f"Error in restore_default_comments: {e}")
            self.logger.error(traceback.format_exc())
            return {'status': 'error', 'message': str(e)}

    def delete_comment_section(self, data):
        """Delete a comment section and its associated comments from DB."""
        try:
            section_name = data.get('section')
            lang = data.get('lang', 'fr')

            if not section_name:
                return {'status': 'error', 'message': "Section name is required."}

            if not re.match(r'^[\w\-\s]+$', section_name):
                return {'status': 'error', 'message': "Invalid section name."}

            count = self.shared_data.db.execute(
                "DELETE FROM comments WHERE status=? AND lang=?;",
                (section_name, lang)
            )
            if count == 0:
                return {'status': 'error', 'message': f"Section '{section_name}' not found for lang='{lang}'."}

            return {'status': 'success', 'message': 'Section deleted successfully.'}
        except Exception as e:
            self.logger.error(f"Error in delete_comment_section: {e}")
            self.logger.error(traceback.format_exc())
            return {'status': 'error', 'message': str(e)}
