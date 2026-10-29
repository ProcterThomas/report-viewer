"""Word-level PDF <-> speech alignment for the read-along GUI.

Builds, per PDF page, a list of words with bounding boxes, flags words
that belong to standalone display equations (so they can be skipped
during narration and highlighting), and synthesizes speech while
recording a start/end timestamp for every spoken word.

Equation detection is a heuristic, not a real math parser: it looks for
LaTeX math-italic/symbol fonts (CMMI/CMSY/CMEX and friends) and only
flags a *cluster* of vertically-adjacent, mostly-math-font lines - a
single inline math symbol (e.g. "20{\\sigma}", "3.8 um") is deliberately
left alone and spoken normally, since those carry real numeric content.
Only compact, dense, multi-line math regions (the shape of a standalone
equation) get flagged. It will miss some equations and, rarely, flag a
short equation-like fragment it shouldn't; it is tuned to avoid ever
dropping ordinary scientific notation from the narration.
"""
from __future__ import annotations

import difflib
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from units import expand_units

MATH_FONT_PATTERNS = (
    "CMMI", "CMSY", "CMEX", "CMBSY", "MSAM", "MSBM",
    "EUFM", "EUSM", "LASY", "RSFS", "MATH", "STIX", "SYMBOL",
)
WORD_MATH_OVERLAP_THRESHOLD = 0.3
STRONG_LINE_FRACTION = 0.85
WEAK_LINE_FRACTION = 0.35
LINE_ADJACENCY_TOLERANCE_PT = 3.0
MIN_EQUATION_CLUSTER_WORDS = 5


@dataclass
class Word:
    text: str
    bbox: tuple[float, float, float, float]
    page: int
    is_equation: bool = False
    # What to synthesize, when it differs from the displayed text (a word
    # broken across lines is spoken whole by its first piece; its second
    # piece then speaks nothing).
    speech: str | None = None

    @property
    def spoken(self) -> str:
        return self.text if self.speech is None else self.speech


@dataclass
class PageWords:
    page: int
    width: float
    height: float
    words: list[Word]
    equation_regions: list[tuple[float, float, float, float]] = field(default_factory=list)


@dataclass
class TimedWord:
    text: str
    page: int | None
    bbox: tuple[float, float, float, float] | None
    start: float
    end: float


@dataclass
class SentenceChunk:
    """One sentence's worth of audio plus its words, timed locally (0-based
    within this chunk) so playback can resync every sentence instead of
    drifting against a single wall-clock timer over the whole document."""

    audio: bytes
    sample_rate: int
    sample_width: int
    channels: int
    words: list[TimedWord]
    page: int


def _is_math_font(fontname: str) -> bool:
    upper = fontname.upper()
    return any(pattern in upper for pattern in MATH_FONT_PATTERNS)


def _overlap_fraction(word_bbox, rect: pymupdf.Rect) -> float:
    word_rect = pymupdf.Rect(word_bbox)
    area = word_rect.get_area()
    if not area:
        return 0.0
    return (word_rect & rect).get_area() / area


def _find_equation_word_indices(page: pymupdf.Page, words: list) -> tuple[set[int], list[pymupdf.Rect]]:
    """Return indices into `words` that belong to a detected equation cluster."""
    math_rects = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                if _is_math_font(span["font"]) and span["text"].strip():
                    math_rects.append(pymupdf.Rect(span["bbox"]))

    lines: dict[tuple[int, int], list[int]] = defaultdict(list)
    for idx, w in enumerate(words):
        lines[(w[5], w[6])].append(idx)

    line_records = []
    for key, idxs in lines.items():
        idxs.sort(key=lambda i: words[i][7])
        flags = [
            max((_overlap_fraction(words[i][:4], r) for r in math_rects), default=0.0)
            >= WORD_MATH_OVERLAP_THRESHOLD
            for i in idxs
        ]
        frac = sum(flags) / len(flags)
        y0 = min(words[i][1] for i in idxs)
        y1 = max(words[i][3] for i in idxs)
        line_records.append({"idxs": idxs, "frac": frac, "y0": y0, "y1": y1})

    line_records.sort(key=lambda rec: rec["y0"])
    n = len(line_records)
    eligible = [rec["frac"] >= WEAK_LINE_FRACTION for rec in line_records]
    strong = [rec["frac"] >= STRONG_LINE_FRACTION for rec in line_records]

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        if not eligible[i]:
            continue
        for j in (i - 1, i + 1):
            if 0 <= j < n and eligible[j]:
                gap = (line_records[i]["y0"] - line_records[j]["y1"]) if j < i else (
                    line_records[j]["y0"] - line_records[i]["y1"]
                )
                if gap <= LINE_ADJACENCY_TOLERANCE_PT:
                    union(i, j)

    components: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        if eligible[i]:
            components[find(i)].append(i)

    equation_word_indices: set[int] = set()
    equation_rects: list[pymupdf.Rect] = []
    for comp in components.values():
        total_words = sum(len(line_records[i]["idxs"]) for i in comp)
        if not any(strong[i] for i in comp):
            continue
        if total_words < MIN_EQUATION_CLUSTER_WORDS:
            continue
        rect = None
        for i in comp:
            for idx in line_records[i]["idxs"]:
                equation_word_indices.add(idx)
                wr = pymupdf.Rect(words[idx][:4])
                rect = wr if rect is None else (rect | wr)
        if rect is not None:
            equation_rects.append(rect)

    equation_word_indices = _fill_small_gaps(equation_word_indices, len(words))
    return equation_word_indices, equation_rects


def _fill_small_gaps(indices: set[int], total: int, max_gap: int = 2) -> set[int]:
    """Absorb short runs of non-equation word indices sandwiched between
    equation-flagged indices (e.g. a stray subscript or equation number)."""
    filled = set(indices)
    sorted_idx = sorted(indices)
    for a, b in zip(sorted_idx, sorted_idx[1:]):
        gap = b - a - 1
        if 0 < gap <= max_gap:
            filled.update(range(a + 1, b))
    return filled


def _equation_marker_words(raw_words: list, start: int, end: int, page_no: int) -> list[Word]:
    rect = None
    for i in range(start, end):
        wr = pymupdf.Rect(raw_words[i][:4])
        rect = wr if rect is None else (rect | wr)
    bbox = tuple(rect) if rect is not None else (0.0, 0.0, 0.0, 0.0)
    return [
        Word(text="Equation", bbox=bbox, page=page_no, is_equation=True),
        Word(text="omitted.", bbox=bbox, page=page_no, is_equation=True),
    ]


def _tokens(phonemes: list[str]) -> list[list[int]]:
    """Indices into `phonemes`, grouped into space-separated tokens."""
    tokens: list[list[int]] = []
    current: list[int] = []
    for i, phoneme in enumerate(phonemes):
        if phoneme == " ":
            if current:
                tokens.append(current)
                current = []
        else:
            current.append(i)
    if current:
        tokens.append(current)
    return tokens


def _label_spoken_phonemes(voice, words: list[Word], sentences: list[list[str]]) -> list[list[int]]:
    """For every phoneme Piper will speak for a page (`sentences`, from
    voice.phonemize(page_text) - the same call voice.synthesize makes), the
    index into `words` of the word it belongs to.

    Phonemizing words one at a time does not predict the in-context
    phonemes: espeak merges unstressed function words ("of the" becomes one
    token) and reads some words differently mid-sentence ("OTAs." is
    spelled out in context but pronounced as a word alone). Counting
    isolated tokens therefore overshot, which pushed every word boundary
    earlier than the real audio, compounding down each page. Instead, the
    isolated phonemes (whose owning word is known) are sequence-aligned
    against the spoken ones: identical tokens map directly, mismatched
    stretches are aligned phoneme by phoneme, and anything left over
    inherits its neighbour's word - so a mismatch stays local to the words
    actually involved instead of shifting everything after it.
    """
    cache: dict[str, list[str]] = {}
    iso_phonemes: list[str] = []
    iso_owner: list[int] = []
    iso_tokens: list[tuple[str, ...]] = []
    iso_spans: list[tuple[int, int]] = []
    for word_idx, word in enumerate(words):
        if word.spoken not in cache:
            phonemes: list[str] = []
            for sentence in voice.phonemize(word.spoken):
                phonemes.extend(sentence)
                phonemes.append(" ")
            cache[word.spoken] = phonemes
        phonemes = cache[word.spoken]
        for token in _tokens(phonemes):
            start = len(iso_phonemes)
            iso_phonemes.extend(phonemes[i] for i in token)
            iso_owner.extend([word_idx] * len(token))
            iso_tokens.append(tuple(phonemes[i] for i in token))
            iso_spans.append((start, len(iso_phonemes)))

    spoken_pos: list[tuple[int, int]] = []
    spoken_tokens: list[tuple[str, ...]] = []
    spoken_spans: list[tuple[int, int]] = []
    for s_idx, sentence in enumerate(sentences):
        for token in _tokens(sentence):
            start = len(spoken_pos)
            spoken_pos.extend((s_idx, i) for i in token)
            spoken_tokens.append(tuple(sentence[i] for i in token))
            spoken_spans.append((start, len(spoken_pos)))

    flat: list[int | None] = [None] * len(spoken_pos)

    def align_phonemes(iso_a: int, iso_b: int, sp_a: int, sp_b: int) -> None:
        a = iso_phonemes[iso_a:iso_b]
        b = [sentences[s][i] for s, i in spoken_pos[sp_a:sp_b]]
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if tag in ("equal", "replace"):
                for k in range(j2 - j1):
                    flat[sp_a + j1 + k] = iso_owner[iso_a + i1 + (k * (i2 - i1)) // (j2 - j1)]

    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, iso_tokens, spoken_tokens, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                owner = iso_owner[iso_spans[i1 + k][0]]
                a, b = spoken_spans[j1 + k]
                flat[a:b] = [owner] * (b - a)
        elif tag == "replace":
            align_phonemes(iso_spans[i1][0], iso_spans[i2 - 1][1], spoken_spans[j1][0], spoken_spans[j2 - 1][1])

    labels: list[list[int | None]] = [[None] * len(sentence) for sentence in sentences]
    for (s_idx, i), owner in zip(spoken_pos, flat):
        labels[s_idx][i] = owner

    # Spaces and unmatched phonemes take the preceding word (the pause after
    # a word belongs to it); anything before the first match takes the first.
    previous: int | None = None
    for sentence_labels in labels:
        for i, owner in enumerate(sentence_labels):
            if owner is None:
                sentence_labels[i] = previous
            else:
                previous = owner
    first_owner = next((o for s in labels for o in s if o is not None), 0)
    return [[first_owner if o is None else o for o in s] for s in labels]


def load_voice(
    model_path: Path, config_path: Path, intra_op_threads: int | None = None, cpu_mem_arena: bool = True
):
    """Load a Piper voice with phoneme alignments enabled.

    `intra_op_threads` caps ONNX's CPU thread pool (default: one per core);
    `cpu_mem_arena=False` stops ONNX keeping freed tensor memory pooled,
    which bounds a long-running process's memory at a small speed cost.
    PiperVoice.load can't take session options, so either means building
    the session the way load() does; this relies on Piper internals, which
    is why the service that uses it pins piper-tts.
    """
    from piper.voice import PiperVoice

    if intra_op_threads is None and cpu_mem_arena:
        return PiperVoice.load(model_path, config_path, use_cuda=False, include_alignments=True)

    import json

    import onnx
    import onnxruntime
    from piper.config import PiperConfig
    from piper.patch_voice_with_alignment import add_alignment_output
    from piper.phonemize_espeak import ESPEAK_DATA_DIR

    model = onnx.load(str(model_path))
    add_alignment_output(model)
    options = onnxruntime.SessionOptions()
    if intra_op_threads is not None:
        options.intra_op_num_threads = intra_op_threads
    options.enable_cpu_mem_arena = cpu_mem_arena
    return PiperVoice(
        config=PiperConfig.from_dict(json.loads(Path(config_path).read_text(encoding="utf-8"))),
        session=onnxruntime.InferenceSession(
            model.SerializeToString(), sess_options=options, providers=["CPUExecutionProvider"]
        ),
        espeak_data_dir=Path(ESPEAK_DATA_DIR),
        download_dir=Path.cwd(),
    )


def utterance_end(words: list[Word], start: int, max_words: int) -> int:
    """End (exclusive) of the words to synthesize for an utterance starting
    at `start`: at most `max_words`, cut after a comma/semicolon/colon in
    the second half of that span when there is one.

    Piper's memory use grows with utterance length, and text without full
    stops (a title plus author list, say) can otherwise become one very long
    "sentence". Ordinary sentences end well before the cap, where Piper
    splits them itself.
    """
    end = min(len(words), start + max_words)
    if end < len(words):
        for j in range(end - 1, start + max_words // 2, -1):
            if words[j].text.endswith((",", ";", ":")):
                return j + 1
    return end


def narrate_words(voice, words: list[Word]):
    """Synthesize `words` (a contiguous run from one page) one sentence at a
    time, yielding a SentenceChunk as soon as each is ready.

    Chunks cover `words` in order with no gaps or overlap: each chunk's
    `words` are the next len(chunk.words) input words, timed locally (0 =
    start of that chunk's audio), and if the generator runs to completion
    every input word is covered. Callers may stop early; whatever was
    yielded is still a valid prefix.

    Every spoken phoneme is tagged with its word by
    `_label_spoken_phonemes`, so which words belong to a chunk, and how
    many of the chunk's audio samples each word gets (from Piper's
    per-phoneme alignment output), both follow from the phonemes actually
    spoken rather than from a predicted count.
    """
    from piper.config import SynthesisConfig

    if not words:
        return
    syn_config = SynthesisConfig()
    text = " ".join(w.spoken for w in words)

    sentences = voice.phonemize(text)
    labels = _label_spoken_phonemes(voice, words, sentences)
    # voice.synthesize skips empty sentences, so pair its chunks with
    # only the non-empty ones.
    spoken_labels = [lab for sentence, lab in zip(sentences, labels) if sentence]

    next_word = 0
    chunks = voice.synthesize(text, syn_config=syn_config, include_alignments=True)
    for n, (chunk, sentence_labels) in enumerate(zip(chunks, spoken_labels)):
        first = next_word
        if n == len(spoken_labels) - 1:
            last = len(words) - 1
        else:
            last = max(first - 1, max(sentence_labels))
        chunk_words = words[first : last + 1]
        next_word = last + 1

        per_word_samples = [0] * len(chunk_words)
        if chunk_words:
            alignments = chunk.phoneme_alignments
            if alignments and len(alignments) == len(sentence_labels) + 2:
                lead = alignments[0].num_samples
                per_phoneme = [a.num_samples for a in alignments[1:-1]]
                tail = alignments[-1].num_samples
            else:
                # Piper couldn't produce alignments for this sentence;
                # spread its audio evenly over its phonemes instead.
                share = (len(chunk.audio_int16_bytes) // 2) // (len(sentence_labels) + 2)
                lead, per_phoneme, tail = share, [share] * len(sentence_labels), share
            per_word_samples[0] += lead
            for owner, n_samples in zip(sentence_labels, per_phoneme):
                per_word_samples[min(max(owner, first), last) - first] += n_samples
            per_word_samples[-1] += tail

        timed_words = []
        offset_samples = 0
        for word, n_samples in zip(chunk_words, per_word_samples):
            start = offset_samples / chunk.sample_rate
            end = start + n_samples / chunk.sample_rate
            timed_words.append(
                TimedWord(
                    text=word.text,
                    page=None if word.is_equation else word.page,
                    bbox=word.bbox if not word.is_equation else None,
                    start=start,
                    end=end,
                )
            )
            offset_samples += n_samples

        yield SentenceChunk(
            audio=chunk.audio_int16_bytes,
            sample_rate=chunk.sample_rate,
            sample_width=chunk.sample_width,
            channels=chunk.sample_channels,
            words=timed_words,
            page=words[0].page,
        )

    if next_word == 0:
        # Nothing speakable (e.g. only punctuation): still cover the words,
        # with a silent zero-length chunk, so callers never wait on them.
        yield SentenceChunk(
            audio=b"",
            sample_rate=voice.config.sample_rate,
            sample_width=2,
            channels=1,
            words=[
                TimedWord(
                    text=w.text,
                    page=None if w.is_equation else w.page,
                    bbox=None if w.is_equation else w.bbox,
                    start=0.0,
                    end=0.0,
                )
                for w in words
            ],
            page=words[0].page,
        )


def stream_narration(
    pages: list[PageWords],
    model_path: Path,
    config_path: Path,
    on_status=lambda msg: None,
):
    """Narrate whole pages from the start of the document, in order."""
    voice = load_voice(model_path, config_path)
    for page in pages:
        if page.words:
            on_status(f"Synthesizing page {page.page + 1}/{len(pages)} ...")
            yield from narrate_words(voice, page.words)
    on_status("Done.")


# Running headers/footers: a text block lying wholly within this fraction of
# the page height from the top or bottom edge, whose text (digits ignored,
# so page numbers match) recurs in the same band on at least half the pages.
RUNNING_BAND_FRACTION = 0.10
_EDGE_PUNCTUATION = ".,;:!?()[]{}\"'\u201c\u201d\u2018\u2019"


def _running_header_footer_blocks(raw_pages: list[tuple[pymupdf.Page, list]]) -> list[set[int]]:
    """Per page, the PyMuPDF block numbers that are running headers/footers.

    These are page furniture ("Internal to Argus ... Do not distribute. 3",
    a journal name, a bare page number), not part of the text: reading them
    aloud on every page interrupts the narration. Some PDF generators
    (ReportLab, for one) also emit the footer first on each page, so it
    would be read before the page's own text.
    """
    keys_per_page: list[dict[int, tuple[str, str]]] = []
    counts: Counter[tuple[str, str]] = Counter()
    for page, raw_words in raw_pages:
        blocks: dict[int, list] = defaultdict(list)
        for w in raw_words:
            blocks[w[5]].append(w)
        height = page.rect.height
        keys = {}
        for block_no, ws in blocks.items():
            # Word boxes are in unrotated page space; bands are about the
            # page as displayed.
            rect = pymupdf.Rect(
                min(w[0] for w in ws), min(w[1] for w in ws), max(w[2] for w in ws), max(w[3] for w in ws)
            ) * page.rotation_matrix
            if rect.y1 <= RUNNING_BAND_FRACTION * height:
                band = "top"
            elif rect.y0 >= (1 - RUNNING_BAND_FRACTION) * height:
                band = "bottom"
            else:
                continue
            text = " ".join(w[4] for w in sorted(ws, key=lambda w: (w[6], w[7])))
            keys[block_no] = (band, " ".join(re.sub(r"\d+", "#", text.lower()).split()))
        keys_per_page.append(keys)
        counts.update(set(keys.values()))
    threshold = max(2, math.ceil(len(raw_pages) / 2))
    return [{b for b, key in keys.items() if counts[key] >= threshold} for keys in keys_per_page]


def _vocabulary_key(text: str) -> str:
    return text.strip(_EDGE_PUNCTUATION).lower()


def _line_break_joins(raw_words: list, skip: set[int], vocabulary: Counter[str]) -> dict[int, str]:
    """Speech overrides that rejoin words hyphenated across a line break.

    "as-" at the end of one line and "semblies" at the start of the next are
    spoken as "assemblies" (by the first piece; the second piece then speaks
    nothing). A hyphen at a line break may also be a real compound's
    ("small-aperture"), and no dictionary is available, so the document is
    the evidence: join when the joined word appears elsewhere in it, or when
    the second piece never appears there as a word of its own; leave it
    when the compound appears elsewhere, or the second piece starts with
    anything but a lowercase letter ("Argus-" / "280").
    """
    speech: dict[int, str] = {}
    for i in range(len(raw_words) - 1):
        first, second = raw_words[i], raw_words[i + 1]
        stem, rest = first[4][:-1], second[4]
        if (
            i in skip
            or i + 1 in skip
            or not first[4].endswith("-")
            or not stem
            or first[5] != second[5]  # same block
            or second[6] != first[6] + 1  # first piece ends its line, second starts the next
            or second[7] != 0
            or not rest[:1].islower()
        ):
            continue
        head, tail = _vocabulary_key(stem), _vocabulary_key(rest)
        if vocabulary[head + tail] or (not vocabulary[f"{head}-{tail}"] and vocabulary[tail] < 2):
            speech[i] = stem + rest
            speech[i + 1] = ""
    return speech


def extract_pages(pdf_path: Path) -> list[PageWords]:
    doc = pymupdf.open(str(pdf_path))
    raw_pages = []
    for page in doc:
        raw_words = page.get_text("words")
        raw_words.sort(key=lambda w: (w[5], w[6], w[7]))
        raw_pages.append((page, raw_words))
    running = _running_header_footer_blocks(raw_pages)
    raw_pages = [
        (page, [w for w in raw_words if w[5] not in drop]) for (page, raw_words), drop in zip(raw_pages, running)
    ]
    vocabulary = Counter(_vocabulary_key(w[4]) for _, raw_words in raw_pages for w in raw_words)

    pages = []
    for page_no, (page, raw_words) in enumerate(raw_pages):
        equation_indices, equation_rects = _find_equation_word_indices(page, raw_words)
        speech = _line_break_joins(raw_words, equation_indices, vocabulary)

        words: list[Word] = []
        run_start: int | None = None
        for i, w in enumerate(raw_words):
            if i in equation_indices:
                if run_start is None:
                    run_start = i
                continue
            if run_start is not None:
                words.extend(_equation_marker_words(raw_words, run_start, i, page_no))
                run_start = None
            words.append(Word(text=w[4], bbox=tuple(w[:4]), page=page_no, speech=speech.get(i)))
        if run_start is not None:
            words.extend(_equation_marker_words(raw_words, run_start, len(raw_words), page_no))
        # Unit symbols are spoken as words ("280 mm" -> "280 millimeters").
        previous = None
        for word in words:
            if not word.is_equation and word.spoken:
                expanded = expand_units(word.spoken, previous)
                if expanded is not None:
                    word.speech = expanded
            previous = word.text
        pages.append(
            PageWords(
                page=page_no,
                width=page.rect.width,
                height=page.rect.height,
                words=words,
                equation_regions=[tuple(r) for r in equation_rects],
            )
        )
    return pages
