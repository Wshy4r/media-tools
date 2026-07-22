#!/usr/bin/env python3
from email import policy
from email.parser import BytesParser
import json
import mimetypes
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ALLOWED_HOST_PARTS = (
    "youtube.com",
    "youtu.be",
    "facebook.com",
    "fb.watch",
    "instagram.com",
    "x.com",
    "twitter.com",
)
RENDER_LOCK = threading.Lock()
RENDER_JOBS = {}
RENDER_JOBS_LOCK = threading.Lock()


class MediaToolsHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/reel-render-progress":
            self._handle_reel_render_progress(parsed)
            return
        if parsed.path == "/api/reel-render-result":
            self._handle_reel_render_result(parsed)
            return
        super().do_GET()

    def do_POST(self):
        if self.path == "/api/reel-render-start":
            self._handle_reel_render_start()
            return

        if self.path == "/api/reel-render":
            self._handle_reel_render()
            return

        if self.path != "/api/reel-download":
            self.send_error(404)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            url = str(payload.get("url", "")).strip()
            self._validate_url(url)
            video_bytes, filename = self._download_video(url)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except subprocess.TimeoutExpired:
            self._send_json(504, {"error": "Download timed out."})
            return
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})
            return

        content_type = mimetypes.guess_type(filename)[0] or "video/mp4"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(video_bytes)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(video_bytes)

    def _handle_reel_render_start(self):
        if not RENDER_LOCK.acquire(blocking=False):
            self._send_json(429, {"error": "A video is already rendering. Please wait for it to finish."})
            return

        try:
            form = self._parse_multipart_form()
            manifest = json.loads(form.get("manifest", b"{}").decode("utf-8"))
            job_id = uuid.uuid4().hex
            with RENDER_JOBS_LOCK:
                RENDER_JOBS[job_id] = {
                    "status": "rendering",
                    "progress": 1,
                    "message": "Starting render...",
                    "result": None,
                    "error": None,
                }
            thread = threading.Thread(target=self._run_reel_render_job, args=(job_id, form, manifest), daemon=True)
            thread.start()
        except Exception as exc:
            RENDER_LOCK.release()
            self._send_json(500, {"error": str(exc)})
            return

        self._send_json(200, {"jobId": job_id})

    def _run_reel_render_job(self, job_id, form, manifest):
        try:
            video_bytes = self._render_reel(form, manifest, job_id)
            self._set_render_job(job_id, status="done", progress=100, message="Done", result=video_bytes)
        except Exception as exc:
            self._set_render_job(job_id, status="error", message=str(exc), error=str(exc))
        finally:
            RENDER_LOCK.release()

    def _handle_reel_render_progress(self, parsed):
        job_id = parse_qs(parsed.query).get("job", [""])[0]
        with RENDER_JOBS_LOCK:
            job = RENDER_JOBS.get(job_id)
            if not job:
                self._send_json(404, {"error": "Render job not found."})
                return
            self._send_json(200, {
                "status": job["status"],
                "progress": job["progress"],
                "message": job["message"],
                "error": job["error"],
            })

    def _handle_reel_render_result(self, parsed):
        job_id = parse_qs(parsed.query).get("job", [""])[0]
        with RENDER_JOBS_LOCK:
            job = RENDER_JOBS.get(job_id)
            if not job:
                self._send_json(404, {"error": "Render job not found."})
                return
            if job["status"] == "error":
                self._send_json(500, {"error": job["error"] or "Render failed."})
                return
            if job["status"] != "done":
                self._send_json(202, {"error": "Render is still running."})
                return
            video_bytes = job["result"]

        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(video_bytes)))
        self.send_header("Content-Disposition", 'attachment; filename="forbes-reel.mp4"')
        self.end_headers()
        self.wfile.write(video_bytes)

    def _set_render_job(self, job_id, **updates):
        with RENDER_JOBS_LOCK:
            if job_id in RENDER_JOBS:
                RENDER_JOBS[job_id].update(updates)

    def _validate_url(self, url):
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if parsed.scheme not in {"http", "https"} or not host:
            raise ValueError("Enter a valid video link.")
        if not any(part in host.lower() for part in ALLOWED_HOST_PARTS):
            raise ValueError("Supported links: Facebook, YouTube, Instagram, X.")

    def _download_video(self, url):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_template = str(Path(tmpdir) / "reel.%(ext)s")
            command = [
                "yt-dlp",
                "--no-playlist",
                "-f",
                "bv*+ba/b[ext=mp4]/best",
                "--merge-output-format",
                "mp4",
                "-o",
                output_template,
                url,
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                message = (result.stderr or result.stdout or "Download failed.").strip().splitlines()[-1]
                raise RuntimeError(message)

            files = sorted(Path(tmpdir).glob("reel.*"), key=lambda path: path.stat().st_size, reverse=True)
            if not files:
                raise RuntimeError("Download finished but no video file was created.")

            video_path = files[0]
            return video_path.read_bytes(), video_path.name

    def _handle_reel_render(self):
        if not RENDER_LOCK.acquire(blocking=False):
            self._send_json(429, {"error": "A video is already rendering. Please wait for it to finish."})
            return
        try:
            form = self._parse_multipart_form()
            manifest = json.loads(form.get("manifest", b"{}").decode("utf-8"))
            video_bytes = self._render_reel(form, manifest)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except subprocess.TimeoutExpired:
            self._send_json(504, {"error": "Render timed out."})
            return
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})
            return
        finally:
            RENDER_LOCK.release()

        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(video_bytes)))
        self.send_header("Content-Disposition", 'attachment; filename="forbes-reel.mp4"')
        self.end_headers()
        self.wfile.write(video_bytes)

    def _render_reel(self, form, manifest, job_id=None):
        clips = manifest.get("clips") or []
        if not clips:
            raise ValueError("Add at least one video first.")

        width = int(manifest.get("width") or 914)
        height = int(manifest.get("height") or 1600)
        logo_path = Path(__file__).resolve().parent / "images" / "LogoW.png"
        if not logo_path.exists():
            raise RuntimeError("Forbes logo file is missing.")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            segment_paths = []
            prepared_clips = []

            for index, clip in enumerate(clips):
                field_name = str(clip.get("field") or f"clip{index}")
                if field_name not in form:
                    raise ValueError(f"Missing video file for clip {index + 1}.")

                input_path = tmp_path / f"input-{index}.mp4"
                input_path.write_bytes(form[field_name])
                duration = self._clip_render_duration(input_path, clip)
                prepared_clips.append((clip, input_path, duration))

            total_duration = sum(item[2] for item in prepared_clips) or len(prepared_clips)
            completed_duration = 0

            for index, (clip, input_path, duration) in enumerate(prepared_clips):
                segment_path = tmp_path / f"segment-{index}.mp4"
                progress_start = 5 + (completed_duration / total_duration) * 90
                progress_span = (duration / total_duration) * 90
                self._set_render_job(job_id, progress=round(progress_start), message=f"Rendering clip {index + 1} of {len(prepared_clips)}...")
                self._render_reel_segment(input_path, segment_path, logo_path, clip, width, height, job_id, progress_start, progress_span, duration)
                completed_duration += duration
                segment_paths.append(segment_path)

            if len(segment_paths) == 1:
                output_path = segment_paths[0]
            else:
                concat_file = tmp_path / "concat.txt"
                concat_file.write_text(
                    "".join(f"file '{path.as_posix()}'\n" for path in segment_paths),
                    encoding="utf-8",
                )
                output_path = tmp_path / "forbes-reel.mp4"
                command = [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_file),
                    "-c",
                    "copy",
                    str(output_path),
                ]
                self._set_render_job(job_id, progress=96, message="Joining clips...")
                self._run_ffmpeg(command)

            return Path(output_path).read_bytes()

    def _render_reel_segment(self, input_path, output_path, logo_path, clip, width, height, job_id=None, progress_start=0, progress_span=0, duration=0):
        trim_enabled = bool(clip.get("trimEnabled"))
        trim_start = max(0.0, float(clip.get("trimStart") or 0))
        trim_end = clip.get("trimEnd")
        duration_args = []
        if trim_enabled:
            if trim_end is not None:
                trim_duration = float(trim_end) - trim_start
                if trim_duration <= 0:
                    raise ValueError("Cut End must be after Cut Start.")
                duration_args = ["-ss", str(trim_start), "-t", str(trim_duration)]
            elif trim_start:
                duration_args = ["-ss", str(trim_start)]

        zoom = max(100.0, float(clip.get("zoom") or 100)) / 100
        move_x = int(float(clip.get("moveX") or 0))
        move_y = int(float(clip.get("moveY") or 0))
        ratio = width / height
        logo_w = 232
        logo_h = 57

        filter_graph = (
            f"[0:v]split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale=if(gt(a\\,{ratio})\\,-2\\,228):if(gt(a\\,{ratio})\\,400\\,-2),"
            f"crop=228:400,boxblur=10:1,scale={width}:{height}[bg];"
            f"[fgsrc]scale=if(gt(a\\,{ratio})\\,{width}\\,-2):if(gt(a\\,{ratio})\\,-2\\,{height}),"
            f"scale=trunc(iw*{zoom}/2)*2:trunc(ih*{zoom}/2)*2[fg];"
            f"[1:v]scale={logo_w}:{logo_h}[logo];"
            f"[bg][fg]overlay=(W-w)/2+{move_x}:(H-h)/2+{move_y}[tmp];"
            f"[tmp][logo]overlay=74:208,format=yuv420p[v]"
        )

        video_codec_args = self._video_codec_args()
        command = [
            "ffmpeg",
            "-y",
            *duration_args,
            "-i",
            str(input_path),
            "-i",
            str(logo_path),
            "-filter_complex",
            filter_graph,
            "-map",
            "[v]",
            "-map",
            "0:a?",
            *video_codec_args,
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-shortest",
            str(output_path),
        ]
        self._run_ffmpeg(command, job_id, progress_start, progress_span, duration)

    def _probe_duration(self, input_path):
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(input_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return 0
        try:
            return max(0, float(result.stdout.strip()))
        except ValueError:
            return 0

    def _clip_render_duration(self, input_path, clip):
        source_duration = self._probe_duration(input_path)
        if not bool(clip.get("trimEnabled")):
            return source_duration or 1
        trim_start = max(0.0, float(clip.get("trimStart") or 0))
        trim_end = clip.get("trimEnd")
        if trim_end is None:
            return max(0.1, (source_duration or trim_start + 1) - trim_start)
        return max(0.1, float(trim_end) - trim_start)

    def _video_codec_args(self):
        encoders = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
        if "h264_videotoolbox" in encoders.stdout:
            return ["-c:v", "h264_videotoolbox", "-b:v", "4500k"]
        return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "26"]

    def _run_ffmpeg(self, command, job_id=None, progress_start=0, progress_span=0, duration=0):
        if not job_id:
            result = subprocess.run(command, capture_output=True, text=True, timeout=600)
            if result.returncode != 0:
                message = (result.stderr or result.stdout or "ffmpeg failed.").strip().splitlines()[-1]
                raise RuntimeError(message)
            return

        progress_command = command[:2] + ["-progress", "pipe:1", "-nostats"] + command[2:]
        process = subprocess.Popen(progress_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        last_lines = []
        try:
            for line in process.stdout:
                line = line.strip()
                if line:
                    last_lines.append(line)
                    last_lines = last_lines[-10:]
                if line.startswith("out_time_ms=") and duration:
                    try:
                        seconds = float(line.split("=", 1)[1]) / 1000000
                        progress = progress_start + min(progress_span, (seconds / duration) * progress_span)
                        self._set_render_job(job_id, progress=max(1, min(99, round(progress))))
                    except ValueError:
                        pass
            return_code = process.wait(timeout=30)
        except Exception:
            process.kill()
            raise
        if return_code != 0:
            raise RuntimeError(last_lines[-1] if last_lines else "ffmpeg failed.")

    def _parse_multipart_form(self):
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            raise ValueError("Expected multipart form data.")

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        raw_message = (
            f"Content-Type: {content_type}\r\n"
            "MIME-Version: 1.0\r\n\r\n"
        ).encode("utf-8") + body
        message = BytesParser(policy=policy.default).parsebytes(raw_message)
        form = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            form[name] = part.get_payload(decode=True) or b""
        return form

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8001
    os.chdir(Path(__file__).resolve().parent)
    server = ThreadingHTTPServer(("localhost", port), MediaToolsHandler)
    print(f"Media tools server running at http://localhost:{port}/index.html")
    server.serve_forever()


if __name__ == "__main__":
    main()
