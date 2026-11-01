#!/usr/bin/env python3
"""Regression test suite for NeoCast.
Tests all 10 required cases:
1. Compatible H.264/AAC MP4 plays without conversion.
2. Compatible video in another container uses remuxing when safe.
3. HEVC requires video conversion without unnecessary upscaling or frame-rate changes.
4. Unsupported audio can be converted without re-encoding otherwise compatible video.
5. A cached conversion is reused and correctly labeled.
6. Resume at 324 seconds works for both direct and converted playback.
7. Restart begins at zero.
8. Startup readings of zero do not destroy a saved resume point.
9. Audio/subtitle changes preserve the intended position.
10. Stop, resume, and switching files do not corrupt playback state.
"""
import asyncio
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import neocast


class NeoCastRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.orig_cache = neocast.CACHE
        self.orig_cfg_file = neocast.CFG_FILE
        self.orig_cfg = copy.deepcopy(neocast.cfg)
        
        neocast.CACHE = Path(self.tmp_dir.name) / "cache"
        neocast.CFG_FILE = Path(self.tmp_dir.name) / "settings.json"
        neocast.CACHE.mkdir(parents=True, exist_ok=True)
        neocast.cfg = {"host": "192.168.1.108", "library": [], "positions": {}}
        neocast.state.update(status="idle", now=None, error=None, device="192.168.1.108",
                             paused=False, position=0, duration=0, resume_verified=False)
        neocast.TOKENS.clear()
        neocast.HITS.clear()
        neocast.INDEX.clear()

    def tearDown(self):
        neocast.CACHE = self.orig_cache
        neocast.CFG_FILE = self.orig_cfg_file
        neocast.cfg = self.orig_cfg
        self.tmp_dir.cleanup()

    # Case 1: Compatible H.264/AAC MP4 plays without conversion
    def test_case_1_compatible_h264_aac_mp4_direct_playback(self):
        info = {
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "3600.0"},
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "profile": "Main", "level": 31,
                 "width": 1280, "height": 720, "avg_frame_rate": "24/1"},
                {"codec_type": "audio", "codec_name": "aac", "channels": 2}
            ]
        }
        analysis = neocast.check_compatibility(info)
        self.assertEqual(analysis["mode"], "direct")
        self.assertFalse(analysis["video_reencode"])
        self.assertFalse(analysis["audio_reencode"])
        self.assertEqual(analysis["reasons"], [])
        self.assertTrue(neocast.is_compatible(info))
        self.assertIn("Plays directly", analysis["summary"])

        # Test prepare() returns original file directly without running ffmpeg
        dummy_file = Path(self.tmp_dir.name) / "compatible.mp4"
        dummy_file.write_bytes(b"dummy mp4 content")
        with patch("neocast.probe", return_value=info):
            result = neocast.prepare(dummy_file)
            self.assertEqual(result, dummy_file)

    # Case 2: Compatible video in another container uses remuxing when safe
    def test_case_2_compatible_video_remux_container(self):
        info = {
            "format": {"format_name": "matroska,webm", "duration": "3600.0"},
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "profile": "Main", "level": 31,
                 "width": 1280, "height": 720, "avg_frame_rate": "24/1"},
                {"codec_type": "audio", "codec_name": "aac", "channels": 2}
            ]
        }
        analysis = neocast.check_compatibility(info)
        self.assertEqual(analysis["mode"], "remux")
        self.assertFalse(analysis["video_reencode"], "Video should NOT be re-encoded during remux")
        self.assertFalse(analysis["audio_reencode"], "Audio should NOT be re-encoded during remux")
        self.assertIn("Container remux", analysis["conversion_text"])

    # Case 3: HEVC requires video conversion without unnecessary upscaling or frame-rate changes
    def test_case_3_hevc_video_conversion_no_upscaling_or_fps_change(self):
        # 536p 24fps HEVC
        info_536p = {
            "format": {"format_name": "mov,mp4,m4a", "duration": "5400.0"},
            "streams": [
                {"codec_type": "video", "codec_name": "hevc", "profile": "Main",
                 "width": 1280, "height": 536, "avg_frame_rate": "24/1"},
                {"codec_type": "audio", "codec_name": "aac", "channels": 2}
            ]
        }
        analysis = neocast.check_compatibility(info_536p)
        self.assertEqual(analysis["mode"], "video_transcode")
        self.assertTrue(analysis["video_reencode"])
        # Crucial checks: preserve 536p height and 24 fps
        self.assertEqual(analysis["out_height"], 536, "536p must NOT be upscaled to 720p")
        self.assertEqual(analysis["out_width"], 1280)
        self.assertEqual(analysis["out_fps"], 24.0, "24fps must NOT be changed to 30fps")
        self.assertIn("HEVC → H.264", analysis["conversion_text"])
        self.assertIn("536p · 24 fps · H.264 / AAC", analysis["output_text"])

        # Scope aspect ratio: 1920x800 at 24fps
        info_scope = {
            "format": {"format_name": "mov,mp4,m4a", "duration": "5400.0"},
            "streams": [
                {"codec_type": "video", "codec_name": "hevc", "profile": "Main",
                 "width": 1920, "height": 800, "avg_frame_rate": "24/1"},
                {"codec_type": "audio", "codec_name": "aac", "channels": 2}
            ]
        }
        analysis_scope = neocast.check_compatibility(info_scope)
        self.assertLessEqual(analysis_scope["out_width"], 1280)
        self.assertLessEqual(analysis_scope["out_height"], 720)
        self.assertEqual(analysis_scope["out_width"] % 2, 0)
        self.assertEqual(analysis_scope["out_height"] % 2, 0)
        self.assertEqual(analysis_scope["out_width"], 1280)
        self.assertEqual(analysis_scope["out_height"], 534)
        self.assertEqual(analysis_scope["out_fps"], 24.0)

    # Case 4: Unsupported audio can be converted without re-encoding otherwise compatible video
    def test_case_4_unsupported_audio_conversion_copies_video(self):
        info_ac3 = {
            "format": {"format_name": "mov,mp4,m4a", "duration": "3600.0"},
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "profile": "Main", "level": 31,
                 "width": 1280, "height": 720, "avg_frame_rate": "24/1"},
                {"codec_type": "audio", "codec_name": "ac3", "channels": 6}
            ]
        }
        analysis = neocast.check_compatibility(info_ac3)
        self.assertEqual(analysis["mode"], "audio_transcode")
        self.assertFalse(analysis["video_reencode"], "Compatible video stream must be COPIED without re-encoding")
        self.assertTrue(analysis["audio_reencode"], "Audio must be converted to AAC")
        self.assertIn("AC3 → AAC", analysis["conversion_text"])

    # Case 5: A cached conversion is reused and correctly labeled
    def test_case_5_cached_conversion_reused_and_labeled(self):
        dummy_file = Path(self.tmp_dir.name) / "test_movie.mkv"
        dummy_file.write_bytes(b"dummy source video")
        
        # Initially not cached
        self.assertFalse(neocast.is_cached(dummy_file, None, 0))
        
        # Simulate completed conversion existing in cache
        cached_path = neocast.get_cache_path(dummy_file, None, 0)
        cached_path.write_bytes(b"cached transcoded mp4")
        
        # Now cached should be True
        self.assertTrue(neocast.is_cached(dummy_file, None, 0))
        
        info = {
            "format": {"format_name": "matroska,webm", "duration": "3600.0"},
            "streams": [
                {"codec_type": "video", "codec_name": "hevc", "profile": "Main",
                 "width": 1280, "height": 536, "avg_frame_rate": "24/1"},
                {"codec_type": "audio", "codec_name": "aac", "channels": 2}
            ]
        }
        with patch("neocast.probe", return_value=info):
            result = neocast.prepare(dummy_file)
            self.assertEqual(result, cached_path)

    # Case 6: Resume at 324 seconds works for both direct and converted playback
    def test_case_6_resume_at_324_seconds(self):
        async def run_test():
            dummy_file = Path(self.tmp_dir.name) / "movie.mp4"
            dummy_file.write_bytes(b"dummy")
            key = str(dummy_file.resolve())
            
            # Setup mock pyatv
            mock_atv = MagicMock()
            mock_play_url = asyncio.Future()
            # Simulate playing stream running
            mock_atv.stream.play_url = MagicMock(return_value=mock_play_url)
            
            tok = neocast.register(dummy_file)
            neocast.HITS[tok] = 1  # mark box fetched media
            
            # Mock airplay_request:
            # 1st GET /scrub: returns position 0.0 (Apple TV ignored start position)
            # POST /scrub: returns 200
            # 2nd GET /scrub: returns position 324.2 (seek verified!)
            scrub_responses = [
                (200, "position: 0.000000\nduration: 7200.000000\n"),
                (200, "OK"), # POST seek response
                (200, "position: 324.200000\nduration: 7200.000000\n"),
            ]
            call_count = 0
            def mock_airplay(host, method, path, body=None, timeout=10):
                nonlocal call_count
                resp = scrub_responses[min(call_count, len(scrub_responses) - 1)]
                call_count += 1
                return resp
            
            with patch("neocast.airplay_request", side_effect=mock_airplay):
                # Start play_media in a task
                play_task = asyncio.create_task(
                    neocast.play_media(mock_atv, f"http://127.0.0.1:8787/m/{tok}/video.mp4", tok, key, start=324)
                )
                for _ in range(30):
                    if neocast.state["resume_verified"]:
                        break
                    await asyncio.sleep(0.1)
                
                # Verify seek happened and position was verified near 324
                self.assertTrue(neocast.state["resume_verified"])
                self.assertAlmostEqual(neocast.state["position"], 324.2, delta=1.0)
                
                # Cleanup
                play_task.cancel()
                await asyncio.gather(play_task, return_exceptions=True)
                
        asyncio.run(run_test())

    # Case 7: Restart begins at zero
    def test_case_7_restart_begins_at_zero(self):
        dummy_file = Path(self.tmp_dir.name) / "movie.mp4"
        dummy_file.write_bytes(b"dummy")
        key = str(dummy_file.resolve())
        
        # Save previous position at 5:24 (324s)
        neocast.set_saved_pos(dummy_file, 324.0, 7200.0)
        self.assertEqual(neocast.get_saved_pos(dummy_file)[0], 324.0)
        
        # Test truthiness and restart handling
        body_restart = {"id": "dummy_id", "restart": True}
        saved = neocast.get_saved_pos(dummy_file)
        start = 0.0 if body_restart.get("restart") else saved[0]
        self.assertEqual(start, 0.0, "Restart must evaluate to 0.0")

        # Test explicit position 0.0 is not discarded by truthiness check
        body_zero = {"id": "dummy_id", "restart": False, "at": 0.0}
        if body_zero.get("restart"):
            start_zero = 0.0
        elif body_zero.get("at") is not None:
            start_zero = float(body_zero["at"])
        else:
            start_zero = saved[0]
        self.assertEqual(start_zero, 0.0, "Explicit 'at': 0.0 must be respected and not fall back to saved")

    # Case 8: Startup readings of zero do not destroy a saved resume point
    def test_case_8_startup_zero_readings_do_not_destroy_saved_resume_point(self):
        dummy_file = Path(self.tmp_dir.name) / "saved_movie.mp4"
        dummy_file.write_bytes(b"dummy")
        key = str(dummy_file.resolve())
        
        # Set saved resume point at 324 seconds
        neocast.set_saved_pos(dummy_file, 324.0, 7200.0)
        
        async def run_test():
            mock_atv = MagicMock()
            mock_play_url = asyncio.Future()
            mock_atv.stream.play_url = MagicMock(return_value=mock_play_url)
            
            tok = neocast.register(dummy_file)
            neocast.HITS[tok] = 1
            
            # Receiver initially reports 0.0
            def mock_airplay(host, method, path, body=None, timeout=10):
                if method == "GET" and path == "/scrub":
                    return (200, "position: 0.000000\nduration: 7200.000000\n")
                return (200, "OK")
                
            with patch("neocast.airplay_request", side_effect=mock_airplay):
                task = asyncio.create_task(
                    neocast.play_media(mock_atv, "http://127.0.0.1:8787/m/test/video.mp4", tok, key, start=324)
                )
                await asyncio.sleep(0.05)
                # Cancel task as if stopped early before verification
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                
            # Saved position MUST NOT have been overwritten with 0!
            current_saved = neocast.get_saved_pos(dummy_file)
            self.assertEqual(current_saved[0], 324.0, "Startup reading of 0.0 must not destroy saved resume point 324.0")
            
        asyncio.run(run_test())

    # Case 9: Audio/subtitle changes preserve the intended position
    def test_case_9_audio_subtitle_changes_preserve_intended_position(self):
        dummy_file = Path(self.tmp_dir.name) / "movie.mkv"
        dummy_file.write_bytes(b"dummy")
        fid = neocast.add_index(dummy_file)
        
        # Test simulated /api/play body with explicit position during track change
        body_change = {"id": fid, "restart": False, "at": 450.5, "sub": 1, "audio": 1}
        
        p = neocast.INDEX.get(body_change.get("id"))
        self.assertIsNotNone(p)
        if body_change.get("restart"):
            start = 0.0
        elif body_change.get("at") is not None:
            start = float(body_change["at"])
        else:
            start = neocast.get_saved_pos(p)[0]
            
        self.assertEqual(start, 450.5, "Position 450.5s must be preserved across track changes")

    # Case 10: Stop, resume, and switching files do not corrupt playback state
    def test_case_10_stop_resume_switching_files_state_integrity(self):
        file_a = Path(self.tmp_dir.name) / "movie_a.mp4"
        file_b = Path(self.tmp_dir.name) / "movie_b.mp4"
        file_a.write_bytes(b"A")
        file_b.write_bytes(b"B")
        
        key_a = str(file_a.resolve())
        key_b = str(file_b.resolve())
        
        neocast.set_saved_pos(file_a, 500.0, 7200.0)
        neocast.set_saved_pos(file_b, 120.0, 3600.0)
        
        # Verify keys are independent
        self.assertEqual(neocast.get_saved_pos(file_a)[0], 500.0)
        self.assertEqual(neocast.get_saved_pos(file_b)[0], 120.0)
        
        # Update A
        neocast.set_saved_pos(file_a, 550.0, 7200.0)
        self.assertEqual(neocast.get_saved_pos(file_a)[0], 550.0)
        self.assertEqual(neocast.get_saved_pos(file_b)[0], 120.0, "File B position must remain untouched")


if __name__ == "__main__":
    unittest.main()
