"""Unit tests for core.voicemail_forward — the single voicemail forward
implementation (Rule 1), shared by tg-voice-agi.py (dialplan h-exten)
and sweep-recordings.py (systemd timer safety net)."""

from __future__ import annotations

from pathlib import Path

from core import voicemail_forward as vfm


class TestClassify:
    def test_below_threshold_is_early_hangup(self):
        assert vfm.classify(2.9, 3) == "early_hangup"

    def test_at_threshold_is_normal(self):
        # int(duration) < max — exactly 3.0 s is NOT early hangup
        assert vfm.classify(3.0, 3) == "normal"

    def test_long_recording_is_normal(self):
        assert vfm.classify(45.2, 3) == "normal"

    def test_custom_threshold(self):
        assert vfm.classify(9.5, 10) == "early_hangup"
        assert vfm.classify(10.2, 10) == "normal"


class TestFfprobeDuration:
    def test_missing_file_is_zero(self, tmp_path):
        assert vfm.ffprobe_duration(str(tmp_path / "nope.wav")) == 0.0


class TestNormalizeVolume:
    def test_falls_back_to_original_when_ffmpeg_fails(self, tmp_path, monkeypatch):
        wav = tmp_path / "vm.wav"
        wav.write_bytes(b"RIFF")
        monkeypatch.setattr(vfm, "_ffmpeg", lambda *a, **k: False)
        temps: list[str] = []
        assert vfm.normalize_volume(str(wav), temps) == str(wav)
        # temp paths are registered so the caller can clean them up
        assert (tmp_path / "vm.opus").as_posix() in temps
        assert (tmp_path / "vm.ogg").as_posix() in temps

    def test_prefers_opus(self, tmp_path, monkeypatch):
        wav = tmp_path / "vm.wav"
        wav.write_bytes(b"RIFF")
        opus = tmp_path / "vm.opus"

        def fake_ffmpeg(input_path, codec, output_path):
            if codec == "libopus":
                Path(output_path).write_bytes(b"OggS")
                return True
            return False

        monkeypatch.setattr(vfm, "_ffmpeg", fake_ffmpeg)
        temps: list[str] = []
        assert vfm.normalize_volume(str(wav), temps) == str(opus)

    def test_ogg_fallback_when_opus_fails(self, tmp_path, monkeypatch):
        wav = tmp_path / "vm.wav"
        wav.write_bytes(b"RIFF")
        ogg = tmp_path / "vm.ogg"

        def fake_ffmpeg(input_path, codec, output_path):
            if codec == "libvorbis":
                Path(output_path).write_bytes(b"OggS")
                return True
            return False

        monkeypatch.setattr(vfm, "_ffmpeg", fake_ffmpeg)
        temps: list[str] = []
        assert vfm.normalize_volume(str(wav), temps) == str(ogg)


class TestForwardRecording:
    def test_missing_file_reports_recording_missing(self, tmp_path, monkeypatch):
        posts: list[tuple] = []
        monkeypatch.setattr(
            vfm, "post_json",
            lambda url, secret, path, payload: (
                posts.append((path, payload)) or (True, "HTTP 200")),
        )

        def no_multipart(*a, **k):
            raise AssertionError("multipart must not be used for a missing file")

        monkeypatch.setattr(vfm, "post_multipart", no_multipart)

        ok, detail, vm_type = vfm.forward_recording(
            str(tmp_path / "missing.wav"), "+79000000000", "corr-1",
            "http://127.0.0.1:1", "sec",
        )
        assert ok is True
        assert vm_type == "recording_missing"
        path, payload = posts[0]
        assert path == "/events/voicemail"
        assert payload == {
            "phone_number": "+79000000000",
            "voicemail_type": "recording_missing",
            "correlation_id": "corr-1",
            "duration": "0.0",
        }

    def test_zero_audio_reports_early_hangup_json(self, tmp_path, monkeypatch):
        wav = tmp_path / "empty.wav"
        wav.write_bytes(b"")
        posts: list[dict] = []
        monkeypatch.setattr(vfm, "ffprobe_duration", lambda p: 0.0)
        monkeypatch.setattr(
            vfm, "post_json",
            lambda url, secret, path, payload: (
                posts.append(payload) or (True, "HTTP 200")),
        )

        def no_multipart(*a, **k):
            raise AssertionError("zero-audio recording must be reported as JSON")

        monkeypatch.setattr(vfm, "post_multipart", no_multipart)

        ok, detail, vm_type = vfm.forward_recording(
            str(wav), "+79000000001", "corr-2", "http://127.0.0.1:1", "sec",
        )
        assert ok is True and vm_type == "early_hangup"
        assert posts[0]["voicemail_type"] == "early_hangup"
        assert wav.exists()  # cleanup is the caller's decision

    def test_failure_returns_not_ok_and_keeps_file(self, tmp_path, monkeypatch):
        wav = tmp_path / "vm.wav"
        wav.write_bytes(b"RIFF-fake")
        monkeypatch.setattr(vfm, "ffprobe_duration", lambda p: 30.0)
        monkeypatch.setattr(vfm, "normalize_volume", lambda p, t: p)
        monkeypatch.setattr(vfm, "post_multipart", lambda *a, **k: (False, "HTTP 500"))

        ok, detail, vm_type = vfm.forward_recording(
            str(wav), "+79000000002", "corr-3", "http://127.0.0.1:1", "sec",
        )
        assert ok is False
        assert vm_type == "normal"
        assert wav.exists()  # kept for retry by the sweeper

    def test_multipart_body_carries_fields_and_file(self, tmp_path, monkeypatch):
        wav = tmp_path / "vm.wav"
        wav.write_bytes(b"RIFF-fake")
        captured: list[tuple] = []
        monkeypatch.setattr(vfm, "ffprobe_duration", lambda p: 30.0)
        monkeypatch.setattr(vfm, "normalize_volume", lambda p, t: p)

        def fake_post(url, secret, path, data, content_type):
            captured.append((path, data, content_type, secret))
            return True, "HTTP 200"

        monkeypatch.setattr(vfm, "_post", fake_post)

        ok, detail, vm_type = vfm.forward_recording(
            str(wav), "+79000000003", "corr-4", "http://127.0.0.1:1", "sec-42",
        )
        assert ok and vm_type == "normal"
        path, data, content_type, secret = captured[0]
        assert path == "/events/voicemail"
        assert "multipart/form-data; boundary=" in content_type
        assert secret == "sec-42"
        body = data.decode("utf-8", "replace")
        assert 'name="phone_number"' in body
        assert "+79000000003" in body
        assert 'name="voicemail_type"' in body
        assert "normal" in body
        assert 'name="file"' in body
        assert "RIFF-fake" in body  # the file bytes are in the body


class TestCleanup:
    def test_removes_file(self, tmp_path):
        wav = tmp_path / "vm.wav"
        wav.write_bytes(b"x")
        vfm.cleanup_recording(str(wav))
        assert not wav.exists()

    def test_missing_file_is_silent(self, tmp_path):
        vfm.cleanup_recording(str(tmp_path / "nope.wav"))  # must not raise
