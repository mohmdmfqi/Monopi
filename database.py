import logging
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


logger = logging.getLogger("monopi.database")


# =========================================================
# DATABASE PATH  (Railway note — read this)
# =========================================================
#
# Railway's container filesystem is EPHEMERAL: any file written
# to disk is wiped on every redeploy, restart, or crash-restart
# UNLESS it lives on an attached Volume. That includes a SQLite
# file sitting next to this script.
#
# To make data survive deploys on Railway:
#   1. Open your service → Settings → Volumes → "Add Volume".
#   2. Mount it at a path, e.g.  /data
#   3. Set the environment variable  DB_PATH=/data/monopi_v2.db
#
# Without a Volume + DB_PATH set, the bot will run fine but every
# user, problem, and solution will be lost on the next deploy.
#
# Locally (no DB_PATH set) it just falls back to a file in the
# project folder, which is fine for development.

# Other hosts (Pterodactyl-style panels such as Botkeep): the server
# folder normally survives restarts, but a redeploy / re-upload can
# still replace or clean it. Two layers protect the data:
#   * keep the DB in its own folder, e.g. DB_PATH=data/monopi_v2.db
#   * rely on the Telegram backups below (BACKUP_INTERVAL_HOURS) --
#     the admin receives the .db file in chat and can /restore it.
DB_PATH = os.getenv("DB_PATH", "monopi_v2.db")

_db_dir = os.path.dirname(DB_PATH)
if _db_dir:
    Path(_db_dir).mkdir(parents=True, exist_ok=True)


# =========================================================
# DATABASE CONNECTION
# =========================================================

@contextmanager
def get_connection():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
    )
    conn.row_factory = sqlite3.Row

    # WAL: readers and writers stop blocking each other, which is
    # the #1 cause of "database is locked" errors under real bot
    # traffic (many users tapping buttons at the same time).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# =========================================================
# INIT DATABASE
# =========================================================

def init_db():

    with get_connection() as conn:

        cursor = conn.cursor()

        # =================================================
        # USERS
        # =================================================

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                full_name TEXT,
                bio TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # =================================================
        # USER SKILLS
        # =================================================

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_skills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                skill TEXT NOT NULL,

                UNIQUE(telegram_id, skill),

                FOREIGN KEY (telegram_id)
                REFERENCES users(telegram_id)
                ON DELETE CASCADE
            )
        """)

        # =================================================
        # PROBLEMS
        # =================================================

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS problems (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                telegram_id INTEGER NOT NULL,

                title TEXT NOT NULL,

                description TEXT NOT NULL,

                category TEXT,

                importance TEXT,

                status TEXT DEFAULT 'open',

                created_at TIMESTAMP
                DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY (telegram_id)
                REFERENCES users(telegram_id)
                ON DELETE CASCADE
            )
        """)

        # =================================================
        # PROBLEM SKILLS
        # =================================================

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS problem_skills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                problem_id INTEGER NOT NULL,

                skill TEXT NOT NULL,

                UNIQUE(problem_id, skill),

                FOREIGN KEY (problem_id)
                REFERENCES problems(id)
                ON DELETE CASCADE
            )
        """)

        # =================================================
        # SOLUTIONS
        # =================================================

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS solutions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                problem_id INTEGER NOT NULL,

                telegram_id INTEGER NOT NULL,

                solution_text TEXT NOT NULL,

                status TEXT DEFAULT 'pending',

                created_at TIMESTAMP
                DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY (problem_id)
                REFERENCES problems(id)
                ON DELETE CASCADE,

                FOREIGN KEY (telegram_id)
                REFERENCES users(telegram_id)
                ON DELETE CASCADE
            )
        """)

        # -------------------------------------------------
        # Migration: admin moderation for solutions
        # -------------------------------------------------
        # Older databases were created before admin review of
        # solutions existed. SQLite has no
        # "ADD COLUMN IF NOT EXISTS", so check manually and add
        # it once — existing rows default to 'pending', meaning
        # they'll simply show up in the admin queue on next run.

        solution_columns = {
            row[1]
            for row in cursor.execute(
                "PRAGMA table_info(solutions)"
            ).fetchall()
        }

        if "admin_status" not in solution_columns:

            cursor.execute("""
                ALTER TABLE solutions
                ADD COLUMN admin_status TEXT NOT NULL DEFAULT 'pending'
            """)

            logger.info(
                "Migrated 'solutions' table: added admin_status column"
            )

        # -------------------------------------------------
        # Migration: required_roles for problems/solutions
        # -------------------------------------------------
        # Free-text, comma-separated list of the roles/skills
        # needed to work on a problem or execute a solution (e.g.
        # "برنامه‌نویس پایتون، طراح UI"), set optionally by
        # whoever created the problem/solution. This is what the
        # self-rating step (see collaboration_ratings below) asks
        # a would-be collaborator to score themselves against —
        # manual/admin-assisted team assembly for now, structured
        # so an AI matcher can consume the same data later.

        problem_columns = {
            row[1]
            for row in cursor.execute(
                "PRAGMA table_info(problems)"
            ).fetchall()
        }

        if "required_roles" not in problem_columns:

            cursor.execute("""
                ALTER TABLE problems
                ADD COLUMN required_roles TEXT
            """)

            logger.info(
                "Migrated 'problems' table: added required_roles column"
            )

        if "required_roles" not in solution_columns:

            cursor.execute("""
                ALTER TABLE solutions
                ADD COLUMN required_roles TEXT
            """)

            logger.info(
                "Migrated 'solutions' table: added required_roles column"
            )

        # =================================================
        # COLLABORATIONS
        # =================================================

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS collaborations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                problem_id INTEGER NOT NULL,

                telegram_id INTEGER NOT NULL,

                status TEXT DEFAULT 'pending',

                created_at TIMESTAMP
                DEFAULT CURRENT_TIMESTAMP,

                UNIQUE(problem_id, telegram_id),

                FOREIGN KEY (problem_id)
                REFERENCES problems(id)
                ON DELETE CASCADE,

                FOREIGN KEY (telegram_id)
                REFERENCES users(telegram_id)
                ON DELETE CASCADE
            )
        """)

        # =================================================
        # COLLABORATION RATINGS
        # =================================================
        # One row per (collaboration, role) — a collaborator's
        # self-assessment (0-10) against each role listed in the
        # problem's/solution's required_roles. This is the data
        # the problem/solution owner uses to assemble a team, and
        # later, what an AI matcher would be trained/prompted on.

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS collaboration_ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                collaboration_id INTEGER NOT NULL,

                role TEXT NOT NULL,

                score INTEGER NOT NULL,

                UNIQUE(collaboration_id, role),

                FOREIGN KEY (collaboration_id)
                REFERENCES collaborations(id)
                ON DELETE CASCADE
            )
        """)

        # =================================================
        # NOTIFICATIONS
        # =================================================

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                telegram_id INTEGER NOT NULL,

                message TEXT NOT NULL,

                is_read INTEGER DEFAULT 0,

                created_at TIMESTAMP
                DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY (telegram_id)
                REFERENCES users(telegram_id)
                ON DELETE CASCADE
            )
        """)

        # =================================================
        # VOTES
        # =================================================
        # Generic — target_type is 'problem' or 'solution', so
        # the same table/functions serve voting on both instead
        # of duplicating a votes_problems / votes_solutions pair.

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                telegram_id INTEGER NOT NULL,

                target_type TEXT NOT NULL,
                target_id INTEGER NOT NULL,

                vote_value INTEGER NOT NULL,

                created_at TIMESTAMP
                DEFAULT CURRENT_TIMESTAMP,

                UNIQUE (
                    telegram_id,
                    target_type,
                    target_id
                ),

                FOREIGN KEY (telegram_id)
                REFERENCES users(telegram_id)
                ON DELETE CASCADE
            )
        """)

        # =================================================
        # INVITES
        # =================================================
        # One row per invitation link an admin creates for a
        # specific person: who they are (invited_name), what they
        # do (job_title) and the one question they must answer the
        # first time they open the bot through the link. The
        # answer lives on the same row -- one link, one person, one
        # answer. `code` is what goes after ?start= in the Telegram
        # deep link (random, so links can't be guessed).

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS invites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                code TEXT UNIQUE NOT NULL,

                invited_name TEXT NOT NULL,

                job_title TEXT NOT NULL,

                question TEXT NOT NULL,

                is_active INTEGER NOT NULL DEFAULT 1,

                created_by INTEGER NOT NULL,

                created_at TIMESTAMP
                DEFAULT CURRENT_TIMESTAMP,

                opened_count INTEGER NOT NULL DEFAULT 0,

                answered_by INTEGER,

                answer TEXT,

                answered_at TIMESTAMP
            )
        """)

    logger.info("Database schema ready at %s", DB_PATH)


# =========================================================
# USERS
# =========================================================

def save_user(
    telegram_id,
    username,
    full_name
):

    with get_connection() as conn:

        conn.execute("""
            INSERT INTO users (
                telegram_id,
                username,
                full_name
            )

            VALUES (?, ?, ?)

            ON CONFLICT(telegram_id)

            DO UPDATE SET

                username = excluded.username,

                full_name = excluded.full_name
        """, (
            telegram_id,
            username,
            full_name
        ))


def get_user(telegram_id):

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT
                telegram_id,
                username,
                full_name,
                bio,
                created_at

            FROM users

            WHERE telegram_id = ?
        """, (telegram_id,))

        return cursor.fetchone()


# =========================================================
# USER SKILLS
# =========================================================

def add_user_skill(
    telegram_id,
    skill
):

    with get_connection() as conn:

        conn.execute("""
            INSERT OR IGNORE INTO user_skills (
                telegram_id,
                skill
            )

            VALUES (?, ?)
        """, (
            telegram_id,
            skill
        ))


def get_user_skills(telegram_id):

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT skill

            FROM user_skills

            WHERE telegram_id = ?

            ORDER BY skill
        """, (telegram_id,))

        return [
            row["skill"]
            for row in cursor.fetchall()
        ]


# =========================================================
# PROBLEMS
# =========================================================

def save_problem(
    telegram_id,
    title,
    description,
    category,
    importance,
    required_roles=None
):

    with get_connection() as conn:

        cursor = conn.execute("""
            INSERT INTO problems (
                telegram_id,
                title,
                description,
                category,
                importance,
                required_roles,
                status
            )

            VALUES (?, ?, ?, ?, ?, ?, 'pending')
        """, (
            telegram_id,
            title,
            description,
            category,
            importance,
            required_roles
        ))

        return cursor.lastrowid


def get_problem(problem_id):

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT
                id,
                telegram_id,
                title,
                description,
                category,
                importance,
                required_roles,
                status,
                created_at

            FROM problems

            WHERE id = ?
        """, (problem_id,))

        return cursor.fetchone()


def update_problem_status(problem_id, status):

    with get_connection() as conn:

        conn.execute("""
            UPDATE problems

            SET status = ?

            WHERE id = ?
        """, (
            status,
            problem_id
        ))


def get_pending_problems():

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT
                id,
                telegram_id,
                title,
                description,
                category,
                importance,
                status,
                created_at

            FROM problems

            WHERE status = 'pending'

            ORDER BY id ASC
        """)

        return cursor.fetchall()


def get_open_problems(
    limit=10,
    offset=0
):

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT
                id,
                telegram_id,
                title,
                description,
                category,
                importance,
                required_roles

            FROM problems

            WHERE status = 'open'

            ORDER BY id DESC

            LIMIT ? OFFSET ?
        """, (
            limit,
            offset
        ))

        return cursor.fetchall()


def get_user_problems(
    telegram_id
):

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT
                id,
                title,
                category,
                importance,
                status,
                created_at

            FROM problems

            WHERE telegram_id = ?

            ORDER BY id DESC
        """, (
            telegram_id,
        ))

        return cursor.fetchall()


# =========================================================
# SOLUTIONS
# =========================================================

def add_solution(
    problem_id,
    telegram_id,
    solution_text,
    required_roles=None
):

    with get_connection() as conn:

        cursor = conn.execute("""
            INSERT INTO solutions (
                problem_id,
                telegram_id,
                solution_text,
                required_roles
            )

            VALUES (?, ?, ?, ?)
        """, (
            problem_id,
            telegram_id,
            solution_text,
            required_roles
        ))

        return cursor.lastrowid


def get_problem_solutions(
    problem_id
):
    """
    Solutions visible to a problem's owner.

    Only admin-approved solutions are returned — a solution
    still awaiting (or rejected by) admin moderation must never
    reach the problem owner's inbox.
    """

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT
                s.id,
                s.problem_id,
                s.telegram_id,
                s.solution_text,
                s.status,
                s.admin_status,
                s.required_roles,
                s.created_at,

                u.full_name,
                u.username

            FROM solutions s

            JOIN users u
                ON s.telegram_id = u.telegram_id

            WHERE s.problem_id = ?

            AND s.admin_status = 'approved'

            ORDER BY s.id DESC
        """, (
            problem_id,
        ))

        return cursor.fetchall()


def get_user_solutions(
    telegram_id
):

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT
                s.id,
                s.problem_id,
                s.solution_text,
                s.status,
                s.admin_status,
                s.created_at,

                p.title

            FROM solutions s

            JOIN problems p
                ON s.problem_id = p.id

            WHERE s.telegram_id = ?

            ORDER BY s.id DESC
        """, (
            telegram_id,
        ))

        return cursor.fetchall()


def update_solution_status(
    solution_id,
    status
):
    """
    The PROBLEM OWNER's decision on a solution (accepted /
    rejected as the chosen solution). Independent from admin
    moderation — see update_solution_admin_status().
    """

    with get_connection() as conn:

        conn.execute("""
            UPDATE solutions

            SET status = ?

            WHERE id = ?
        """, (
            status,
            solution_id
        ))


# =========================================================
# SOLUTIONS — ADMIN MODERATION
# =========================================================

def get_pending_admin_solutions():
    """
    Solutions awaiting admin moderation, oldest first — mirrors
    get_pending_problems(). Includes the submitter's name and the
    parent problem's title so the admin has context without an
    extra lookup.
    """

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT
                s.id,
                s.problem_id,
                s.telegram_id,
                s.solution_text,
                s.status,
                s.admin_status,
                s.created_at,

                u.full_name,
                u.username,

                p.title AS problem_title

            FROM solutions s

            JOIN users u
                ON s.telegram_id = u.telegram_id

            JOIN problems p
                ON s.problem_id = p.id

            WHERE s.admin_status = 'pending'

            ORDER BY s.id ASC
        """)

        return cursor.fetchall()


def update_solution_admin_status(
    solution_id,
    admin_status
):
    """
    The ADMIN's content-moderation decision on a solution
    (approved / rejected). Approval is what makes a solution
    visible to the problem owner via get_problem_solutions().
    """

    with get_connection() as conn:

        conn.execute("""
            UPDATE solutions

            SET admin_status = ?

            WHERE id = ?
        """, (
            admin_status,
            solution_id
        ))


# =========================================================
# COLLABORATIONS
# =========================================================

def add_collaboration(
    problem_id,
    telegram_id
):

    with get_connection() as conn:

        try:

            conn.execute("""
                INSERT INTO collaborations (
                    problem_id,
                    telegram_id
                )

                VALUES (?, ?)
            """, (
                problem_id,
                telegram_id
            ))

            return True

        except sqlite3.IntegrityError:

            return False


def get_collaboration(
    problem_id,
    telegram_id
):

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT
                id,
                problem_id,
                telegram_id,
                status,
                created_at

            FROM collaborations

            WHERE problem_id = ?

            AND telegram_id = ?
        """, (
            problem_id,
            telegram_id
        ))

        return cursor.fetchone()


def get_problem_collaborators(
    problem_id
):

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT
                c.id,
                c.telegram_id,
                c.status,
                c.created_at,

                u.full_name,
                u.username

            FROM collaborations c

            JOIN users u
                ON c.telegram_id = u.telegram_id

            WHERE c.problem_id = ?

            ORDER BY c.id DESC
        """, (
            problem_id,
        ))

        return cursor.fetchall()


def update_collaboration_status(
    collaboration_id,
    status
):

    with get_connection() as conn:

        conn.execute("""
            UPDATE collaborations

            SET status = ?

            WHERE id = ?
        """, (
            status,
            collaboration_id
        ))


def get_collaboration_count(
    problem_id
):

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT COUNT(*)

            FROM collaborations

            WHERE problem_id = ?
        """, (
            problem_id,
        ))

        return cursor.fetchone()[0]


def get_user_collaborations(
    telegram_id
):

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT
                c.id,
                c.problem_id,
                c.status,
                c.created_at,

                p.title

            FROM collaborations c

            JOIN problems p
                ON c.problem_id = p.id

            WHERE c.telegram_id = ?

            ORDER BY c.id DESC
        """, (
            telegram_id,
        ))

        return cursor.fetchall()


# =========================================================
# MATCHING
# =========================================================

def find_matching_users(
    problem_id
):

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT
                u.telegram_id,
                u.full_name,
                u.username,

                COUNT(ps.skill) AS matched_skills

            FROM users u

            JOIN user_skills us
                ON u.telegram_id = us.telegram_id

            JOIN problem_skills ps
                ON us.skill = ps.skill

            WHERE ps.problem_id = ?

            GROUP BY
                u.telegram_id,
                u.full_name,
                u.username

            ORDER BY matched_skills DESC
        """, (
            problem_id,
        ))

        return cursor.fetchall()


# =========================================================
# PROBLEM SKILLS
# =========================================================

def add_problem_skill(
    problem_id,
    skill
):

    with get_connection() as conn:

        conn.execute("""
            INSERT OR IGNORE INTO problem_skills (
                problem_id,
                skill
            )

            VALUES (?, ?)
        """, (
            problem_id,
            skill
        ))


def get_problem_skills(
    problem_id
):

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT skill

            FROM problem_skills

            WHERE problem_id = ?

            ORDER BY skill
        """, (
            problem_id,
        ))

        return [
            row["skill"]
            for row in cursor.fetchall()
        ]


# =========================================================
# NOTIFICATIONS
# =========================================================

def add_notification(
    telegram_id,
    message
):

    with get_connection() as conn:

        conn.execute("""
            INSERT INTO notifications (
                telegram_id,
                message
            )

            VALUES (?, ?)
        """, (
            telegram_id,
            message
        ))


def get_unread_notifications(
    telegram_id
):

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT
                id,
                message,
                created_at

            FROM notifications

            WHERE telegram_id = ?

            AND is_read = 0

            ORDER BY id DESC
        """, (
            telegram_id,
        ))

        return cursor.fetchall()


def mark_notifications_read(
    telegram_id
):

    with get_connection() as conn:

        conn.execute("""
            UPDATE notifications

            SET is_read = 1

            WHERE telegram_id = ?
        """, (
            telegram_id,
        ))


# =========================================================
# VOTES
# =========================================================
#
# target_type is always 'problem' or 'solution'; target_id is
# the corresponding problems.id or solutions.id. One vote per
# (user, target) — casting again with a new value overwrites the
# old one; the caller decides when to call remove_vote() instead
# (e.g. tapping the same button again = toggle off).

def cast_vote(
    telegram_id,
    target_type,
    target_id,
    vote_value
):
    """
    vote_value: 1 (👍) or -1 (👎)
    """

    with get_connection() as conn:

        conn.execute("""
            INSERT INTO votes (
                telegram_id,
                target_type,
                target_id,
                vote_value
            )

            VALUES (?, ?, ?, ?)

            ON CONFLICT(telegram_id, target_type, target_id)

            DO UPDATE SET

                vote_value = excluded.vote_value,

                created_at = CURRENT_TIMESTAMP
        """, (
            telegram_id,
            target_type,
            target_id,
            vote_value
        ))


def remove_vote(
    telegram_id,
    target_type,
    target_id
):

    with get_connection() as conn:

        conn.execute("""
            DELETE FROM votes

            WHERE telegram_id = ?

            AND target_type = ?

            AND target_id = ?
        """, (
            telegram_id,
            target_type,
            target_id
        ))


def get_user_vote(
    telegram_id,
    target_type,
    target_id
):
    """
    Returns 1, -1, or None (no vote cast).
    """

    with get_connection() as conn:

        row = conn.execute("""
            SELECT vote_value

            FROM votes

            WHERE telegram_id = ?

            AND target_type = ?

            AND target_id = ?
        """, (
            telegram_id,
            target_type,
            target_id
        )).fetchone()

        return row["vote_value"] if row else None


def get_vote_counts(
    target_type,
    target_id
):
    """
    Returns {"up": int, "down": int, "score": int}.
    """

    with get_connection() as conn:

        row = conn.execute("""
            SELECT
                COALESCE(SUM(
                    CASE WHEN vote_value = 1 THEN 1 ELSE 0 END
                ), 0) AS up,

                COALESCE(SUM(
                    CASE WHEN vote_value = -1 THEN 1 ELSE 0 END
                ), 0) AS down

            FROM votes

            WHERE target_type = ?

            AND target_id = ?
        """, (
            target_type,
            target_id
        )).fetchone()

        up = row["up"]
        down = row["down"]

        return {
            "up": up,
            "down": down,
            "score": up - down
        }


# =========================================================
# ADMIN — DASHBOARD
# =========================================================

def get_admin_stats():
    """
    One-shot snapshot of platform activity for the admin panel's
    📊 آمار screen. A handful of small COUNT queries — cheap
    enough to just run fresh every time the admin opens it rather
    than caching, given the traffic this bot expects.
    """

    with get_connection() as conn:

        def count(query, params=()):
            return conn.execute(query, params).fetchone()[0]

        return {
            "users": count(
                "SELECT COUNT(*) FROM users"
            ),

            "problems_total": count(
                "SELECT COUNT(*) FROM problems"
            ),
            "problems_pending": count(
                "SELECT COUNT(*) FROM problems "
                "WHERE status = 'pending'"
            ),
            "problems_open": count(
                "SELECT COUNT(*) FROM problems "
                "WHERE status = 'open'"
            ),
            "problems_rejected": count(
                "SELECT COUNT(*) FROM problems "
                "WHERE status = 'rejected'"
            ),

            "solutions_total": count(
                "SELECT COUNT(*) FROM solutions"
            ),
            "solutions_pending_review": count(
                "SELECT COUNT(*) FROM solutions "
                "WHERE admin_status = 'pending'"
            ),
            "solutions_approved": count(
                "SELECT COUNT(*) FROM solutions "
                "WHERE admin_status = 'approved'"
            ),
            "solutions_rejected": count(
                "SELECT COUNT(*) FROM solutions "
                "WHERE admin_status = 'rejected'"
            ),
            "solutions_accepted_by_owner": count(
                "SELECT COUNT(*) FROM solutions "
                "WHERE status = 'accepted'"
            ),

            "collaborations_total": count(
                "SELECT COUNT(*) FROM collaborations"
            ),
            "collaborations_pending": count(
                "SELECT COUNT(*) FROM collaborations "
                "WHERE status = 'pending'"
            ),
            "collaborations_accepted": count(
                "SELECT COUNT(*) FROM collaborations "
                "WHERE status = 'accepted'"
            ),

            "votes_total": count(
                "SELECT COUNT(*) FROM votes"
            ),
        }


def get_all_collaborations(
    limit=20
):
    """
    Most recent collaboration requests platform-wide, for the
    admin panel's read-only 🤝 overview. Accepting/rejecting a
    request is still the problem owner's call (see the existing
    "applicants_" flow) — this is visibility only, not a second
    approval gate.
    """

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT
                c.id,
                c.problem_id,
                c.telegram_id,
                c.status,
                c.created_at,

                u.full_name,
                u.username,

                p.title AS problem_title

            FROM collaborations c

            JOIN users u
                ON c.telegram_id = u.telegram_id

            JOIN problems p
                ON c.problem_id = p.id

            ORDER BY c.id DESC

            LIMIT ?
        """, (
            limit,
        ))

        return cursor.fetchall()


# =========================================================
# USERS — PROFILE EDITING
# =========================================================

def update_user_bio(
    telegram_id,
    bio
):

    with get_connection() as conn:

        conn.execute("""
            UPDATE users

            SET bio = ?

            WHERE telegram_id = ?
        """, (
            bio,
            telegram_id
        ))


def replace_user_skills(
    telegram_id,
    skills
):
    """
    Overwrites a user's entire skill list with the given one —
    the free-text skill entry replaces the old fixed-menu flow,
    where each tap only ever added a skill and there was no way
    to remove one. Retyping the list is now how removal works.
    """

    with get_connection() as conn:

        conn.execute("""
            DELETE FROM user_skills

            WHERE telegram_id = ?
        """, (
            telegram_id,
        ))

        for skill in skills:

            conn.execute("""
                INSERT OR IGNORE INTO user_skills (
                    telegram_id,
                    skill
                )

                VALUES (?, ?)
            """, (
                telegram_id,
                skill
            ))


# =========================================================
# USERS — ADMIN DIRECTORY
# =========================================================

def get_all_users(
    limit=1000
):

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT
                telegram_id,
                username,
                full_name,
                bio,
                created_at

            FROM users

            ORDER BY id DESC

            LIMIT ?
        """, (
            limit,
        ))

        return cursor.fetchall()


def get_user_stats(
    telegram_id
):
    """
    Small activity snapshot for the admin's per-user profile
    view — cheap COUNT queries, same pattern as get_admin_stats().
    """

    with get_connection() as conn:

        problems = conn.execute("""
            SELECT COUNT(*) FROM problems
            WHERE telegram_id = ?
        """, (telegram_id,)).fetchone()[0]

        solutions = conn.execute("""
            SELECT COUNT(*) FROM solutions
            WHERE telegram_id = ?
        """, (telegram_id,)).fetchone()[0]

        collaborations = conn.execute("""
            SELECT COUNT(*) FROM collaborations
            WHERE telegram_id = ?
        """, (telegram_id,)).fetchone()[0]

        return {
            "problems": problems,
            "solutions": solutions,
            "collaborations": collaborations,
        }

# =========================================================
# MANUAL TEAM MATCHING
# =========================================================
#
# Manual/admin-assisted by design for now: a problem or solution
# can list the roles it needs (required_roles, free text), and
# when someone offers to collaborate, they self-rate against that
# list. Nothing here auto-assigns anyone — the owner reads the
# ratings and decides. The (role, score) shape is deliberately
# structured so an AI matcher can consume the exact same data
# later without a schema change.

def get_accepted_solution(
    problem_id
):
    """
    The solution the problem owner has picked as "the plan" (if
    any) — its required_roles is what a collaborator gets asked
    to self-rate against, since that's the concrete work, not the
    more open-ended problem itself.
    """

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT
                id,
                problem_id,
                telegram_id,
                solution_text,
                required_roles

            FROM solutions

            WHERE problem_id = ?

            AND status = 'accepted'

            AND admin_status = 'approved'

            ORDER BY id DESC

            LIMIT 1
        """, (
            problem_id,
        ))

        return cursor.fetchone()


def save_collaboration_rating(
    collaboration_id,
    role,
    score
):

    with get_connection() as conn:

        conn.execute("""
            INSERT INTO collaboration_ratings (
                collaboration_id,
                role,
                score
            )

            VALUES (?, ?, ?)

            ON CONFLICT(collaboration_id, role)

            DO UPDATE SET

                score = excluded.score
        """, (
            collaboration_id,
            role,
            score
        ))


def get_collaboration_ratings(
    collaboration_id
):

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT
                role,
                score

            FROM collaboration_ratings

            WHERE collaboration_id = ?

            ORDER BY id ASC
        """, (
            collaboration_id,
        ))

        return cursor.fetchall()


# =========================================================
# INVITES  (admin-created links with a question for one person)
# =========================================================

INVITE_CODE_PREFIX = "inv_"


def create_invite(
    created_by,
    invited_name,
    job_title,
    question
):
    """
    Creates an invite and returns (invite_id, code). The code is
    random and short enough for Telegram's 64-character limit on
    the /start parameter (letters, digits, "_" and "-" only).
    """

    for _ in range(5):

        code = INVITE_CODE_PREFIX + secrets.token_urlsafe(6)

        try:

            with get_connection() as conn:

                cursor = conn.execute("""
                    INSERT INTO invites (
                        code,
                        invited_name,
                        job_title,
                        question,
                        created_by
                    )

                    VALUES (?, ?, ?, ?, ?)
                """, (
                    code,
                    invited_name,
                    job_title,
                    question,
                    created_by
                ))

                return cursor.lastrowid, code

        except sqlite3.IntegrityError:

            # astronomically unlikely code collision -- retry
            continue

    raise RuntimeError("Could not generate a unique invite code")


def get_invite(invite_id):

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT *
            FROM invites
            WHERE id = ?
        """, (
            invite_id,
        ))

        return cursor.fetchone()


def get_invite_by_code(code):

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT *
            FROM invites
            WHERE code = ?
        """, (
            code,
        ))

        return cursor.fetchone()


def increment_invite_opens(invite_id):

    with get_connection() as conn:

        conn.execute("""
            UPDATE invites
            SET opened_count = opened_count + 1
            WHERE id = ?
        """, (
            invite_id,
        ))


def count_invites():

    with get_connection() as conn:

        return conn.execute(
            "SELECT COUNT(*) FROM invites"
        ).fetchone()[0]


def list_invites(
    limit=5,
    offset=0
):
    """Newest first."""

    with get_connection() as conn:

        cursor = conn.execute("""
            SELECT *
            FROM invites
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """, (
            limit,
            offset
        ))

        return cursor.fetchall()


def save_invite_answer(
    invite_id,
    telegram_id,
    answer
):
    """
    Stores the answer only if the invite hasn't been answered yet
    (first answer wins). Returns True if this call saved it.
    """

    with get_connection() as conn:

        cursor = conn.execute("""
            UPDATE invites
            SET
                answer = ?,
                answered_by = ?,
                answered_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND answer IS NULL
        """, (
            answer,
            telegram_id,
            invite_id
        ))

        return cursor.rowcount > 0


def set_invite_active(
    invite_id,
    is_active
):

    with get_connection() as conn:

        conn.execute("""
            UPDATE invites
            SET is_active = ?
            WHERE id = ?
        """, (
            1 if is_active else 0,
            invite_id
        ))


# =========================================================
# BACKUP / RESTORE
# =========================================================
# create_backup() makes a consistent snapshot of the live database
# with SQLite's online backup API (safe while the bot is running,
# WAL included). The bot sends these files to the admins in
# Telegram, so a copy of the data always lives OUTSIDE the host.
# restore_from_file() loads such a file back in.

BACKUP_DIR = os.getenv("BACKUP_DIR", "").strip() or os.path.join(
    os.path.dirname(os.path.abspath(DB_PATH)),
    "backups",
)

try:
    BACKUP_KEEP = max(1, int(os.getenv("BACKUP_KEEP", "10")))
except ValueError:
    BACKUP_KEEP = 10

REQUIRED_TABLES = ("users", "problems", "solutions")


class BackupError(Exception):
    """A backup file is unusable. The message is user-facing (Persian)."""


def _prune_backups(prefix, keep):

    try:
        files = sorted(
            f for f in os.listdir(BACKUP_DIR)
            if f.startswith(prefix + "_") and f.endswith(".db")
        )
    except OSError:
        return

    for old_name in files[:-keep]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old_name))
        except OSError:
            logger.warning("Could not delete old backup %s", old_name)


def create_backup(prefix="monopi_backup"):
    """Snapshot the live DB into BACKUP_DIR; returns the file path."""

    Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(BACKUP_DIR, f"{prefix}_{stamp}.db")

    src = sqlite3.connect(DB_PATH, timeout=30)

    try:

        dst = sqlite3.connect(path)

        try:
            src.backup(dst)
            # single self-contained file (no -wal/-shm beside it)
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()

    finally:
        src.close()

    _prune_backups(prefix, BACKUP_KEEP if prefix == "monopi_backup" else 5)

    logger.info("Backup created: %s", path)

    return path


def inspect_backup_file(path):
    """
    Validate a candidate backup file and return its row counts.
    Raises BackupError (Persian message) if it is not a usable
    Monopi database.
    """

    try:

        conn = sqlite3.connect(
            Path(path).resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=10,
        )

    except sqlite3.Error:
        raise BackupError("فایل قابل باز شدن نیست.")

    try:

        try:

            check = conn.execute("PRAGMA integrity_check").fetchone()[0]

            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }

        except sqlite3.DatabaseError:
            raise BackupError("این فایل یک دیتابیس SQLite معتبر نیست.")

        if check != "ok":
            raise BackupError("فایل آسیب دیده است (integrity check ناموفق).")

        missing = [t for t in REQUIRED_TABLES if t not in tables]

        if missing:
            raise BackupError(
                "این فایل دیتابیس Monopi نیست (جدول‌های اصلی را ندارد)."
            )

        return {
            "users": conn.execute(
                "SELECT COUNT(*) FROM users").fetchone()[0],
            "problems": conn.execute(
                "SELECT COUNT(*) FROM problems").fetchone()[0],
            "solutions": conn.execute(
                "SELECT COUNT(*) FROM solutions").fetchone()[0],
        }

    finally:
        conn.close()


def restore_from_file(path):
    """
    Replace the live database content with the backup at `path`.
    A safety copy of the CURRENT database is made first
    (prefix "pre_restore"). Returns (counts_of_backup, safety_path).
    """

    info = inspect_backup_file(path)

    safety = create_backup(prefix="pre_restore")

    src = sqlite3.connect(path)

    try:

        dst = sqlite3.connect(DB_PATH, timeout=30)

        try:
            src.backup(dst)
        finally:
            dst.close()

    finally:
        src.close()

    # An older backup may lack newer tables/columns -- re-run the
    # idempotent schema setup/migrations.
    init_db()

    logger.info("Database restored from %s (safety copy: %s)", path, safety)

    return info, safety
