"""main.py's final RESULTS print must not crash the whole run when an AI
reply contains characters the console encoding can't represent (e.g. an
emoji on a Windows GBK/cp936 console). The DAG, blackboard and events are
already complete by then -- a summary print is no reason to exit 1.

No framework -- plain assert, exit 1 on failure.
Run: python tests/test_console_encoding.py
"""
from __future__ import annotations

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main


def _gbk_console() -> io.TextIOWrapper:
    """A stdout-like stream that behaves like a Windows GBK console:
    characters outside cp936 raise instead of being silently dropped."""
    return io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict",
                            write_through=True)


def test_gbk_console_crashes_on_emoji_without_guard():
    """Precondition/documentation: a strict GBK stream really does raise on
    an emoji -- this is the exact crash we are guarding against."""
    stream = _gbk_console()
    raised = False
    try:
        stream.write("game \U0001f579 over")  # 🕹
    except UnicodeEncodeError:
        raised = True
    assert raised, "expected a strict GBK stream to raise on the emoji"
    print("PASS: test_gbk_console_crashes_on_emoji_without_guard")


def test_make_output_crashproof_replaces_unencodable_chars():
    """After the guard, printing an emoji to a GBK console must NOT crash;
    the unencodable char is replaced while the surrounding text survives."""
    stream = _gbk_console()
    original = sys.stdout
    sys.stdout = stream
    try:
        main._make_output_crashproof()
        print("game \U0001f579 over")  # would raise UnicodeEncodeError before the guard
        sys.stdout.flush()
    finally:
        sys.stdout = original

    text = stream.buffer.getvalue().decode("gbk", errors="replace")
    assert "game" in text and "over" in text, \
        f"surrounding summary text must survive, got {text!r}"
    assert "\U0001f579" not in text, "the emoji cannot survive GBK; it must be replaced"
    print("PASS: test_make_output_crashproof_replaces_unencodable_chars")


if __name__ == "__main__":
    test_gbk_console_crashes_on_emoji_without_guard()
    test_make_output_crashproof_replaces_unencodable_chars()
    print("\nAll console-encoding tests passed.")
