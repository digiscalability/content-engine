"""Windows notification (SRD §13, D11, C17): a toast whose click opens the digest."""

from pathlib import Path

from engine.ops.notify import FakeNotifier, ToastNotifier, toast_for


def test_the_toast_opens_the_digest_when_clicked(tmp_path):
    digest = tmp_path / "reports" / "2026-11-20.md"
    toast = toast_for("Content engine", "2 drafts to approve", digest)
    assert toast.text_fields == ["Content engine", "2 drafts to approve"]
    assert toast.launch_action == digest.resolve().as_uri()  # works after the engine exits


def test_the_fake_records_what_it_would_show():
    fake = FakeNotifier()
    fake.notify("Content engine", "1 hold", Path("r.md"))
    assert fake.shown == [("Content engine", "1 hold", Path("r.md"))]


def test_the_toast_notifier_names_the_app():
    assert ToastNotifier().app == "content-engine"


def test_loading_the_toast_library_first_does_not_break_opentimelineio():
    """winrt bundles an old msvcp140.dll (14.29); if it loads first, extensions built with
    a newer MSVC (opentimelineio) crash. notify.py loads the system's runtime first."""
    import subprocess
    import sys

    code = (
        "from pathlib import Path\n"
        "from engine.ops import notify\n"
        "notify.toast_for('t', 'b', Path('r.md'))\n"  # imports windows_toasts and winrt
        "import opentimelineio\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.strip() == "ok", result.stderr[-500:]
