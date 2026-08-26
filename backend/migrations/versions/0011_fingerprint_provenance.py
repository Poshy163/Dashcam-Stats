"""Remember which bytes the fingerprint was taken from, so a changed file is noticed.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-26

The scanner's escalation ladder is "an unchanged stat costs one ``stat()``; a changed one
costs a partial fingerprint; a changed fingerprint costs a reprocess". The middle rung was
missing, and nothing said so.

``recordings.size_bytes`` and ``recordings.mtime_ns`` are what the stability check compares
against, so the scan rewrites them on every pass — which means "the stat has moved" is only
ever true on the *one* scan that saw it move. That same scan, correctly, holds the file back
for one stable observation and returns before it reaches the fingerprint. By the next scan
the stat matches the values that scan just wrote, so the cheap path takes over and the file
is never read again.

The consequences were both silent:

* a recording whose file was replaced kept the analysis of the bytes that are no longer
  there — and ``app.ingest.puller`` replaces a short local copy with the complete one every
  time a transfer is cut off mid-file, so this is ordinary operation, not a corner case;
* a recording that had never been analysed was moved to ``settling`` by the scan that saw
  the change and stayed there, invisible to ``queue_unprocessed`` (which looks for
  ``discovered``/``metadata_extracted``) and excluded from every bulk rebuild.

The fix needs one durable fact that the stability columns cannot carry: the stat the stored
fingerprint was actually taken from. These two columns are it.

**The backfill matters as much as the columns.** Left null, every recording in an existing
library would look un-fingerprinted on the next scan and be read again — three sample reads
per file, so a twenty-thousand-file library would do tens of gigabytes of I/O on the first
scan after an upgrade for no result. So rows that already hold a fingerprint are stamped
with the stat they currently carry, which is what the fingerprint was taken from for every
row that is not mid-change.

The one deliberate exception is ``state = 'settling'``. A fingerprinted row in that state is
either a file genuinely still being written, or one of the rows this bug stranded. Leaving
its provenance null costs one fingerprint read and is the only thing that gets the stranded
ones moving again — the file is read once, found to differ, and requeued; or found
identical, and put back to ``discovered``.

The downgrade drops the columns. Nothing derived is lost: without them the scanner simply
returns to its previous behaviour.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.models import RecordingState

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

#: How ``recordings.state`` spells "still being written" on disk.
#:
#: Read from the application enum rather than restated, because the stored representation
#: is the member *name* and the member *value* is the lower-case string that reads naturally
#: in a WHERE clause. A migration is exactly where those two get confused.
_SETTLING = RecordingState.SETTLING.name


def upgrade() -> None:
    op.add_column("recordings", sa.Column("fingerprint_size_bytes", sa.Integer(), nullable=True))
    op.add_column("recordings", sa.Column("fingerprint_mtime_ns", sa.Integer(), nullable=True))

    # One statement over an indexed-by-nothing table is still the cheapest form of this:
    # it touches only rows that hold a fingerprint, and it is a single pass.
    # ``SETTLING``, upper case, because SQLAlchemy's ``Enum`` persists the member *name*
    # rather than its value -- and getting that wrong here is not a cosmetic slip. The
    # predicate would exclude nothing, every stranded row would be stamped with the
    # provenance of the bytes it never read, and the rescue this migration exists for would
    # be a no-op on precisely the library that needs it. Taken from the enum rather than
    # written out, so it cannot drift from the model.
    op.execute(
        sa.text(
            """
            UPDATE recordings
               SET fingerprint_size_bytes = size_bytes,
                   fingerprint_mtime_ns = mtime_ns
             WHERE fingerprint IS NOT NULL
               AND state <> :settling
            """
        ).bindparams(settling=_SETTLING)
    )


def downgrade() -> None:
    op.drop_column("recordings", "fingerprint_mtime_ns")
    op.drop_column("recordings", "fingerprint_size_bytes")
