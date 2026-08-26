"""Australian licence-plate normalisation.

OCR of a plate seen for a fraction of a second at highway speed is never exact. The value
of this module is not that it corrects errors -- it cannot know the truth -- but that it
decides, against the real Australian plate formats, whether a correction is *plausible*,
and refuses to invent one when it is not.

Three rules govern everything here:

* **The raw OCR value is never overwritten.** :class:`NormalisedPlate` carries ``raw``
  alongside ``normalised``, and ``PlateObservation`` stores both columns. A user looking at
  a suspicious reading must be able to see exactly what the model produced.
* **A substitution is only applied when it produces a match.** ``O`` becomes ``0`` in a
  digit slot only because some known format wants a digit there. A confusion resolved for
  its own sake is a fabrication.
* **No match means no state.** When the cleaned text fits nothing, ``matched`` is False,
  ``pattern_name`` and ``state_hint`` stay null, and ``confidence_delta`` is negative. The
  application never asserts an uncertain plate is correct.

The format table is **approximate and deliberately overlapping**. Australia has no single
plate authority; each state and territory issues its own series, formats are reused across
jurisdictions (``AAA000`` has been general issue in at least six of them), personalised
plates ignore the formats entirely, and older series stay on the road for decades. A
matched pattern therefore constrains the *shape* of a plate, and only occasionally its
origin -- which is why ``state_hint`` is populated only for the handful of masks that
exactly one jurisdiction uses, and ``plausible_states`` reports the rest without choosing.

Mask notation, used throughout::

    A  -- one letter slot   (A-Z)
    #  -- one digit slot    (0-9)
    any other character -- a literal that must appear verbatim

``A`` and ``#`` are reserved by that notation, so a mask cannot express a literal ``A`` or
a literal ``#``. No Australian format needs one; the only literal in the table is the
leading ``S`` of the South Australian ``S000AAA`` series.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace

__all__ = [
    "AU_PATTERNS",
    "CONFUSABLE_GROUPS",
    "NormalisedPlate",
    "PlatePattern",
    "clean",
    "normalise",
    "patterns_for_region",
    "plate_similarity",
]

LETTER_SLOT = "A"
DIGIT_SLOT = "#"


# --------------------------------------------------------------------------------------
# Format table
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlatePattern:
    """One plate layout.

    ``states`` lists every jurisdiction known to have issued the layout. A single entry
    means the mask identifies its origin; more than one means it does not, and the caller
    is told so rather than being given a guess.
    """

    name: str
    mask: str
    states: tuple[str, ...] = ()
    #: Lower sorts first when two masks fit equally well, so a specific state series is
    #: preferred over the generic shape fallbacks.
    priority: int = 1

    @property
    def length(self) -> int:
        return len(self.mask)

    @property
    def state_hint(self) -> str | None:
        return self.states[0] if len(self.states) == 1 else None

    @property
    def generic(self) -> bool:
        return not self.states


def _generic_six() -> tuple[PlatePattern, ...]:
    """Every letters-then-digits and digits-then-letters split of a six-character plate.

    These exist so a six-character read that is not one of the named series still resolves
    its confusions and normalises consistently -- ``0`` in an obvious letter run becomes
    ``O`` -- while claiming nothing about where the plate is from. ``AAA###`` and ``###AAA``
    are omitted: they are named series above and would otherwise be duplicated at a lower
    priority.

    The two blanket masks are omitted as well, and for a different reason: they constrain
    nothing. ``AAAAAA`` accepts *any* six-letter word, so the word TOYOTA read off a
    tailgate badge was stored as a plate at 0.99 confidence, matching ``AU:GEN:AAAAAA``.
    ``000000`` is the same hole for digits. A pattern catalogue whose job is to say "this
    has the shape of a registration" cannot include an entry that says "any six characters
    of one kind", because that is not a shape.
    """
    named = {"AAA###", "###AAA"}
    blanket = {LETTER_SLOT * 6, DIGIT_SLOT * 6}
    named = named | blanket
    masks: list[str] = []
    for letters in range(7):
        masks.append(LETTER_SLOT * letters + DIGIT_SLOT * (6 - letters))
        if 0 < letters < 6:
            masks.append(DIGIT_SLOT * (6 - letters) + LETTER_SLOT * letters)
    seen: set[str] = set()
    out: list[PlatePattern] = []
    for mask in masks:
        if mask in named or mask in seen:
            continue
        seen.add(mask)
        out.append(PlatePattern(name=f"AU:GEN:{_display_mask(mask)}", mask=mask, priority=2))
    return tuple(out)


def _display_mask(mask: str) -> str:
    """Render a mask in the ``AAA000`` notation people actually write plate formats in."""
    return "".join("0" if ch == DIGIT_SLOT else ch for ch in mask)


#: Current and recent general-issue series. Approximate, overlapping, and not exhaustive --
#: see the module docstring.
AU_PATTERNS: tuple[PlatePattern, ...] = (
    # -- masks exactly one jurisdiction uses -----------------------------------------
    # SA's current series is the only one with a literal leading letter.
    PlatePattern("SA:S000AAA", "S###AAA", ("SA",), 0),
    PlatePattern("WA:0AAA000", f"{DIGIT_SLOT}AAA###", ("WA",), 0),
    PlatePattern("TAS:A00AA", "A##AA", ("TAS",), 0),
    PlatePattern("ACT:AAA00A", "AAA##A", ("ACT",), 0),
    # -- masks shared between jurisdictions ------------------------------------------
    # NSW's current series; the Northern Territory issues the same shape.
    PlatePattern("AU:AA00AA", "AA##AA", ("NSW", "NT"), 1),
    # The most widely reused Australian format there has ever been.
    PlatePattern("AU:AAA000", "AAA###", ("NSW", "QLD", "SA", "TAS", "VIC", "WA"), 1),
    PlatePattern("AU:000AAA", "###AAA", ("QLD", "VIC"), 1),
    # -- general six-character fallbacks ---------------------------------------------
    *_generic_six(),
)

# --------------------------------------------------------------------------------------
# Character confusions
# --------------------------------------------------------------------------------------

#: Digit -> the letters that share its printed shape, most likely first.
#:
#: ``0`` is genuinely ambiguous: on the narrow European-style typeface used across most
#: Australian series it collides with ``O``, ``D`` and ``Q``. Pattern matching cannot
#: separate them -- all three are letters, so all three satisfy a letter slot equally --
#: so the order here is the tie-break, and it is ranked by how often each confusion is
#: actually observed. The uncertainty that remains is what ``confidence_delta`` is for.
_DIGIT_TO_LETTERS: dict[str, tuple[str, ...]] = {
    "0": ("O", "D", "Q"),
    "1": ("I",),
    "2": ("Z",),
    "5": ("S",),
    "6": ("G",),
    "8": ("B",),
}

#: Letter -> the digit it collides with. Unambiguous in this direction.
_LETTER_TO_DIGITS: dict[str, tuple[str, ...]] = {
    "O": ("0",),
    "D": ("0",),
    "Q": ("0",),
    "I": ("1",),
    "Z": ("2",),
    "S": ("5",),
    "G": ("6",),
    "B": ("8",),
}

#: Characters that look alike regardless of which slot they sit in. Used by
#: :func:`plate_similarity` so two reads that differ only by a shape collision are treated
#: as near-identical rather than as different plates.
CONFUSABLE_GROUPS: tuple[frozenset[str], ...] = (
    frozenset("0ODQ"),
    frozenset("1I"),
    frozenset("2Z"),
    frozenset("5S"),
    frozenset("6G"),
    frozenset("8B"),
)

_SHAPE_GROUP: dict[str, int] = {
    ch: index for index, group in enumerate(CONFUSABLE_GROUPS) for ch in group
}


def _confusable(a: str, b: str) -> bool:
    group = _SHAPE_GROUP.get(a)
    return group is not None and group == _SHAPE_GROUP.get(b)


# --------------------------------------------------------------------------------------
# Cleaning
# --------------------------------------------------------------------------------------

#: Words printed on Australian plates that are not part of the registration. Matched as
#: whole tokens only, so a personalised plate reading ``STATE`` as its entire text is not
#: silently erased -- see :func:`clean`.
_SLOGAN_WORDS = frozenset(
    {
        "AUSTRALIA",
        "AUSTRALIAN",
        "AUS",
        "NEW",
        "SOUTH",
        "WALES",
        "WESTERN",
        "NORTHERN",
        "TERRITORY",
        "CAPITAL",
        "VICTORIA",
        "QUEENSLAND",
        "TASMANIA",
        "CANBERRA",
        "THE",
        "STATE",
        "FESTIVAL",
        "PLACE",
        "TO",
        "BE",
        "SUNSHINE",
        "SMART",
        "EXPLORE",
        "POSSIBILITIES",
        "OUTBACK",
        "NATURALLY",
        "GARDEN",
        "HOLIDAY",
        "ISLE",
        "INSPIRATION",
        "DISCOVER",
        "SPIRIT",
        "HEART",
        "NATION",
        "GOVERNMENT",
        "GOV",
    }
)

#: Dropped only when another token survives; on its own, a two-letter token is far more
#: likely to be part of a plate than a jurisdiction stamp.
_STATE_ABBREVIATIONS = frozenset({"NSW", "VIC", "QLD", "SA", "WA", "TAS", "NT", "ACT"})

#: Longest and shortest text that could plausibly be a registration. Outside this the
#: reading is treated as junk rather than as an unmatched plate.
MIN_PLAUSIBLE_LENGTH = 4
MAX_PLAUSIBLE_LENGTH = 8


def _tokenise(raw: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    for ch in raw.upper():
        if ch.isalnum():
            current.append(ch)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tokens


def clean(raw: str) -> str:
    """Upper-case, drop slogans and separators, and keep only ``A-Z0-9``.

    Slogan removal is done at the *token* level, never by substring: stripping the letters
    ``S`` and ``A`` out of a plate because "SA" appears somewhere in the string would
    destroy the registration it is meant to expose. A token is only discarded when at
    least one other token survives, so a reading that is nothing but a slogan word comes
    back unchanged rather than empty.
    """
    tokens = _tokenise(raw)
    if not tokens:
        return ""

    kept = [t for t in tokens if t not in _SLOGAN_WORDS]
    if not kept:
        kept = tokens

    without_state = [t for t in kept if t not in _STATE_ABBREVIATIONS]
    if without_state:
        kept = without_state

    return "".join(kept)


# --------------------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------------------

#: Confidence adjustments the caller adds to the OCR score. Small by design: normalisation
#: is corroborating evidence about a reading's shape, not a second opinion on its content.
EXACT_MATCH_BONUS = 0.05
SUBSTITUTION_COST = 0.02
#: A shape-only fallback match says much less than a named series, so it earns less.
GENERIC_MATCH_FACTOR = 0.5
NO_MATCH_PENALTY = -0.10
IMPLAUSIBLE_LENGTH_PENALTY = -0.15


@dataclass(frozen=True, slots=True)
class NormalisedPlate:
    """The outcome of normalising one OCR reading.

    ``raw`` is the untouched input and is what ``PlateObservation.raw_text`` stores.
    ``normalised`` is the identity key -- ``Plate.normalised_text`` -- and falls back to
    the cleaned text when nothing matched, so unmatched plates still deduplicate against
    each other instead of every reading creating a new identity.
    """

    raw: str
    normalised: str
    matched: bool
    pattern_name: str | None
    state_hint: str | None
    confidence_delta: float
    #: The input after case folding, slogan removal and separator stripping, before any
    #: confusion was resolved. Kept so the UI can show what changed.
    cleaned: str = ""
    #: How many characters the match required changing. Zero means the reading already fit.
    substitutions: int = 0
    #: Every jurisdiction known to issue the matched layout. More than one entry means the
    #: format does not identify a state, which is why ``state_hint`` stays null.
    plausible_states: tuple[str, ...] = field(default_factory=tuple)

    @property
    def display_text(self) -> str:
        """What ``Plate.display_text`` should hold."""
        return self.normalised or self.raw.strip().upper()

    @property
    def changed(self) -> bool:
        return self.normalised != self.cleaned


# --------------------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------------------

#: Ceiling on the substitution search. Only characters with a shape collision branch, and
#: only ``0`` branches more than two ways, so a real plate never approaches this; it exists
#: so a pathological OCR string cannot turn into a combinatorial explosion.
_MAX_CANDIDATES = 512


def patterns_for_region(region: str | None) -> tuple[PlatePattern, ...]:
    """Patterns applicable to a ``plates.region`` setting value.

    ``AU`` uses the whole table. ``AU-SA`` narrows the named series to South Australia but
    keeps the generic shape fallbacks, so a Victorian car photographed in Adelaide still
    normalises -- it simply does not get a state attributed to it. Anything else disables
    pattern matching entirely.
    """
    if not region:
        return ()
    token = region.strip().upper()
    if token in ("NONE", "OFF", ""):
        return ()
    if token == "AU":
        return AU_PATTERNS
    if token.startswith("AU-"):
        state = token[3:]
        # A named series from elsewhere keeps its *shape* and loses its attribution, rather
        # than disappearing. Dropping it outright left nothing to match a common interstate
        # plate: `AAA###` and `###AAA` are deliberately excluded from the generic fallbacks
        # because they are named series, so scoping to one state removed the only pattern
        # of that shape and a Victorian car photographed in Adelaide matched nothing at all
        # -- while a *wrong* generic shape was still free to claim it. The docstring above
        # has always promised the opposite.
        scoped = []
        for pattern in AU_PATTERNS:
            if pattern.generic or state in pattern.states:
                scoped.append(pattern)
            else:
                scoped.append(replace(pattern, states=(), priority=2))
        return tuple(scoped)
    # An unrecognised region is treated as "no regional rules" rather than as Australia:
    # applying the wrong country's formats would produce confident nonsense.
    return ()


def _slot_options(char: str, slot: str) -> tuple[tuple[str, int, int], ...]:
    """Characters that can occupy ``slot``, as ``(char, substitution_cost, rank)``.

    Rank orders equally valid alternatives by how often the confusion is observed; it only
    ever breaks ties, because every alternative satisfies the slot identically.
    """
    if slot == LETTER_SLOT:
        if char.isalpha():
            return ((char, 0, 0),)
        return tuple(
            (letter, 1, rank) for rank, letter in enumerate(_DIGIT_TO_LETTERS.get(char, ()))
        )
    if slot == DIGIT_SLOT:
        if char.isdigit():
            return ((char, 0, 0),)
        return tuple((digit, 1, rank) for rank, digit in enumerate(_LETTER_TO_DIGITS.get(char, ())))
    # A literal. It may still be reached through a confusion -- SA plates whose leading
    # 'S' was read as '5' are the case this exists for.
    if char == slot:
        return ((slot, 0, 0),)
    if _confusable(char, slot):
        return ((slot, 1, 0),)
    return ()


def _project(cleaned: str, pattern: PlatePattern) -> tuple[str, int, int] | None:
    """Best resolution of ``cleaned`` onto ``pattern``, or None if it cannot fit.

    Returns ``(text, substitutions, rank_total)``. Candidates are enumerated rather than
    resolved greedily so that a literal slot -- which a greedy pass would satisfy with the
    wrong alternative -- is decided by the same scoring as everything else.
    """
    if len(cleaned) != pattern.length:
        return None

    per_slot: list[tuple[tuple[str, int, int], ...]] = []
    combinations = 1
    for char, slot in zip(cleaned, pattern.mask, strict=True):
        options = _slot_options(char, slot)
        if not options:
            return None
        combinations *= len(options)
        if combinations > _MAX_CANDIDATES:
            return None
        per_slot.append(options)

    best: tuple[str, int, int] | None = None
    for combination in itertools.product(*per_slot):
        text = "".join(item[0] for item in combination)
        subs = sum(item[1] for item in combination)
        rank = sum(item[2] for item in combination)
        if best is None or (subs, rank) < (best[1], best[2]):
            best = (text, subs, rank)
    return best


def _confidence_delta(pattern: PlatePattern, substitutions: int) -> float:
    bonus = EXACT_MATCH_BONUS * (GENERIC_MATCH_FACTOR if pattern.generic else 1.0)
    return round(max(NO_MATCH_PENALTY, bonus - substitutions * SUBSTITUTION_COST), 4)


def normalise(raw: str, region: str = "AU") -> NormalisedPlate:
    """Resolve one OCR reading against the known plate formats.

    Never raises and never discards the input: an unreadable or unmatched value comes back
    with ``matched=False``, the cleaned text as ``normalised``, and a negative
    ``confidence_delta``. ``raw`` is returned exactly as given.
    """
    cleaned = clean(raw)
    if not cleaned:
        return NormalisedPlate(
            raw=raw,
            normalised="",
            matched=False,
            pattern_name=None,
            state_hint=None,
            confidence_delta=IMPLAUSIBLE_LENGTH_PENALTY,
            cleaned="",
        )

    candidates: list[tuple[int, int, int, int, str, PlatePattern]] = []
    for index, pattern in enumerate(patterns_for_region(region)):
        if pattern.length != len(cleaned):
            continue
        # A pattern with digit slots has to be shown at least one real digit.
        #
        # Whether a digit slot is satisfied by an actual digit depends only on the cleaned
        # character, not on which projection wins, so it can be decided before projecting.
        # Without it a mask made mostly of letters accepted any word of the right shape by
        # converting its remaining characters: NISSAN off a badge matched ``AA00AA`` and was
        # stored as "NI55AN"; SCHOOL and DANGER off road signs matched ``0AAAAA`` as
        # "5CHOOL" and "0ANGER". Each became a plate identity with its own card, its own
        # rollups and a positive confidence delta. This is the same class of defect the
        # blanket ``AAAAAA``/``000000`` masks were removed for, one step further down.
        #
        # Genuine reads are untouched: they carry at least one character the recogniser saw
        # as a digit -- ``ABC1O3`` still resolves to ``ABC103`` on the strength of its 1 and
        # 3 -- because a plate whose *every* digit was misread as a letter is a different
        # and far rarer thing than a word that never had one.
        if DIGIT_SLOT in pattern.mask and not any(
            slot == DIGIT_SLOT and char.isdigit()
            for char, slot in zip(cleaned, pattern.mask, strict=True)
        ):
            continue
        projected = _project(cleaned, pattern)
        if projected is None:
            continue
        text, substitutions, rank = projected
        # Fewest changes first, then the most specific pattern, then the most commonly
        # observed reading of an ambiguous character.
        candidates.append((substitutions, pattern.priority, rank, index, text, pattern))

    if not candidates:
        implausible = not (MIN_PLAUSIBLE_LENGTH <= len(cleaned) <= MAX_PLAUSIBLE_LENGTH)
        return NormalisedPlate(
            raw=raw,
            # The cleaned text is kept as the identity so repeated sightings of an
            # unmatched plate still collapse onto one row.
            normalised=cleaned,
            matched=False,
            pattern_name=None,
            state_hint=None,
            confidence_delta=IMPLAUSIBLE_LENGTH_PENALTY if implausible else NO_MATCH_PENALTY,
            cleaned=cleaned,
        )

    substitutions, _priority, _rank, _index, text, pattern = min(candidates)
    return NormalisedPlate(
        raw=raw,
        normalised=text,
        matched=True,
        pattern_name=pattern.name,
        state_hint=pattern.state_hint,
        confidence_delta=_confidence_delta(pattern, substitutions),
        cleaned=cleaned,
        substitutions=substitutions,
        plausible_states=pattern.states,
    )


# --------------------------------------------------------------------------------------
# Similarity
# --------------------------------------------------------------------------------------

#: A substitution between two characters that share a printed shape. Half cost, because
#: ``S1ABC`` and ``51ABC`` are overwhelmingly the same plate read twice, not two plates.
_CONFUSION_SUB_COST = 0.5
_SUB_COST = 1.0
_INDEL_COST = 1.0


def _weighted_levenshtein(a: str, b: str) -> float:
    previous = [j * _INDEL_COST for j in range(len(b) + 1)]
    for i, ca in enumerate(a, start=1):
        current = [i * _INDEL_COST]
        for j, cb in enumerate(b, start=1):
            if ca == cb:
                cost = 0.0
            elif _confusable(ca, cb):
                cost = _CONFUSION_SUB_COST
            else:
                cost = _SUB_COST
            current.append(
                min(
                    previous[j] + _INDEL_COST,
                    current[j - 1] + _INDEL_COST,
                    previous[j - 1] + cost,
                )
            )
        previous = current
    return previous[-1]


def plate_similarity(a: str, b: str) -> float:
    """Normalised edit distance in ``0..1``; 1.0 means identical.

    Both sides are cleaned first, so ``ABC-123`` and ``abc 123`` compare as equal, and
    character pairs that share a printed shape cost half a substitution. Intended for
    collapsing near-identical reads of the same car across recordings -- a threshold around
    0.85 separates "same plate, one character misread" from "different plate".
    """
    x, y = clean(a), clean(b)
    if not x and not y:
        return 1.0
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    distance = _weighted_levenshtein(x, y)
    return max(0.0, 1.0 - distance / max(len(x), len(y)))
