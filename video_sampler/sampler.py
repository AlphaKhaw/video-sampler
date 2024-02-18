import os
import time
from collections import Counter
from collections.abc import Iterable
from copy import deepcopy
from queue import Queue
from threading import Thread

import av
from PIL import Image

from .buffer import SamplerConfig, create_buffer
from .gating import create_gate
from .language.keyword_capture import subtitle_line
from .logging import Color, console
from .schemas import PROCESSING_DONE_ITERABLE, FrameObject


class VideoSampler:
    """
    The fundamental class for sampling video frames.

    Args:
        cfg (SamplerConfig): The configuration for the video sampler.

    Attributes:
        cfg (SamplerConfig): The configuration for the video sampler.
        frame_buffer (FrameBuffer): The frame buffer used for sampling frames.
        gate (Gate): The gate used for filtering frames.
        stats (Counter): A counter for tracking statistics.

    Methods:
        sample(video_path) -> Iterable[list[FrameObject]]:
            Generates sample frames from a video.
        write_queue(video_path, q):
            Writes sampled frames to a queue.

    """

    def __init__(self, cfg: SamplerConfig) -> None:
        self.cfg = deepcopy(cfg)
        self.frame_buffer = create_buffer(self.cfg.buffer_config)
        self.gate = create_gate(self.cfg.gate_config)
        self.stats = Counter()

    def flush_buffer(self):
        """Flushes the frame buffer and yields gated frames"""
        for res in self.frame_buffer.final_flush():
            if res:
                self.stats["produced"] += 1
                gated_obj = self.gate(*res)
                self.stats["gated"] += gated_obj.N
                if gated_obj.frames:
                    yield gated_obj.frames
        gated_obj = self.gate.flush()
        self.stats["gated"] += gated_obj.N
        if gated_obj.frames:
            yield gated_obj.frames
        yield PROCESSING_DONE_ITERABLE

    def sample(self, video_path: str) -> Iterable[list[FrameObject]]:
        """Generate sample frames from a video.

        Args:
            video_path (str): The path to the video file.

        Yields:
            Iterable[list[FrameObject]]: A generator that yields a list of FrameObjects representing sampled frames.
        """
        self.stats.clear()
        self.frame_buffer.clear()
        with av.open(video_path) as container:
            stream = container.streams.video[0]
            if self.cfg.keyframes_only:
                stream.codec_context.skip_frame = "NONKEY"
            prev_time = -10
            for frame_indx, frame in enumerate(container.decode(stream)):
                if frame is None:
                    continue
                try:
                    ftime = frame.time
                except AttributeError:
                    continue
                # skip frames if keyframes_only is True
                time_diff = ftime - prev_time
                self.stats["total"] += 1
                if time_diff < self.cfg.min_frame_interval_sec:
                    continue
                prev_time = ftime

                frame_pil: Image = frame.to_image()
                if self.cfg.debug:
                    buf = self.frame_buffer.get_buffer_state()
                    console.print(
                        f"Frame {frame_indx}\ttime: {ftime}",
                        f"\t Buffer ({len(buf)}): {buf}",
                        style=f"bold {Color.green.value}",
                    )
                frame_meta = {"frame_time": ftime, "frame_indx": frame_indx}
                self.stats["decoded"] += 1
                if res := self.frame_buffer.add(
                    frame_pil,
                    metadata=frame_meta,
                ):
                    gated_obj = self.gate(*res)
                    self.stats["produced"] += 1
                    self.stats["gated"] += gated_obj.N
                    if gated_obj.frames:
                        yield gated_obj.frames

        # flush buffer
        yield from self.flush_buffer()

    def write_queue(self, video_path: str, q: Queue):
        try:
            item: tuple[FrameObject, int]
            for item in self.sample(video_path=video_path):
                q.put(item)
        except (av.IsADirectoryError, av.InvalidDataError) as e:
            console.print(
                f"Error while processing {video_path}",
                f"\n\t{e}",
                style=f"bold {Color.red.value}",
            )
            q.put(PROCESSING_DONE_ITERABLE)


class SegmentSampler(VideoSampler):
    def __init__(
        self, cfg: SamplerConfig, segment_generator: Iterable[subtitle_line]
    ) -> None:
        super().__init__(cfg)
        self.segment_generator: Iterable[subtitle_line] = segment_generator

    def sample(self, video_path: str) -> Iterable[list[FrameObject]]:
        """Generate sample frames from a video.

        Args:
            video_path (str): The path to the video file.

        Yields:
            Iterable[list[FrameObject]]: A generator that yields a list of FrameObjects representing sampled frames.
        """
        self.stats.clear()
        self.frame_buffer.clear()
        next_segment = next(self.segment_generator)
        segment_boundary_end_sec = next_segment.end_time / 1000
        segment_boundary_start_sec = next_segment.start_time / 1000
        absolute_stop = False
        with av.open(video_path) as container:
            stream = container.streams.video[0]
            if self.cfg.keyframes_only:
                stream.codec_context.skip_frame = "NONKEY"
            prev_time = -10
            for frame_indx, frame in enumerate(container.decode(stream)):
                if frame is None:
                    continue
                try:
                    ftime = frame.time
                except AttributeError:
                    continue
                reiters = 0
                # find the next segment that starts after the current frame
                while ftime > segment_boundary_end_sec:
                    console.print(
                        f"Seeking to next segment: {segment_boundary_end_sec}/{ftime}",
                        style=f"bold {Color.yellow.value}",
                    )
                    try:
                        next_segment = next(self.segment_generator)
                        reiters += 1
                        segment_boundary_end_sec = next_segment.end_time / 1000
                        segment_boundary_start_sec = next_segment.start_time / 1000
                    except StopIteration:
                        absolute_stop = True
                        break
                if reiters > 0:
                    console.print(
                        f"Skipped {reiters} segments!",
                        style=f"bold {Color.red.value}",
                    )
                if absolute_stop:
                    break
                # we haven't found the next segment yet
                # the other condition, is where we are after the segment
                # but this is handled by the while loop above
                if ftime <= segment_boundary_start_sec:
                    continue

                self.stats["total"] += 1
                time_diff = ftime - prev_time
                if time_diff < self.cfg.min_frame_interval_sec:
                    continue
                prev_time = ftime

                frame_pil: Image = frame.to_image()
                if self.cfg.debug:
                    buf = self.frame_buffer.get_buffer_state()
                    console.print(
                        f"Frame {frame_indx}\ttime: {ftime}",
                        f"\t Buffer ({len(buf)}): {buf}",
                        style=f"bold {Color.green.value}",
                    )
                frame_meta = {"frame_time": ftime, "frame_indx": frame_indx}
                self.stats["decoded"] += 1
                if res := self.frame_buffer.add(
                    frame_pil,
                    metadata=frame_meta,
                ):
                    gated_obj = self.gate(*res)
                    self.stats["produced"] += 1
                    self.stats["gated"] += gated_obj.N
                    if gated_obj.frames:
                        yield gated_obj.frames

        # flush buffer
        yield from self.flush_buffer()

    def write_queue(self, video_path: str, q: Queue):
        super().write_queue(video_path, q)


class Worker:
    def __init__(
        self,
        cfg: SamplerConfig,
        devnull: bool = False,
        processor_cls: VideoSampler = VideoSampler,
        extra_processor_args: dict = None,
    ) -> None:
        if extra_processor_args is None:
            extra_processor_args = {}
        self.cfg = cfg
        self.processor = processor_cls(cfg=cfg, **extra_processor_args)
        self.q = Queue()
        self.devnull = devnull

    def launch(
        self, video_path: str, output_path: str = "", pretty_video_name: str = ""
    ) -> None:
        """
        Launch the worker.

        Args:
            video_path (str): Path to the video file.
            output_path (str, optional): Path to the output folder. Defaults to "".
            pretty_video_name (str, optional): Name of the video file for pretty printing (useful for urls).
                                                Defaults to "".
        """
        if not pretty_video_name:
            pretty_video_name = os.path.basename(video_path)
        if output_path and self.devnull:
            raise ValueError("Cannot write to disk when devnull is True")
        if output_path:
            os.makedirs(output_path, exist_ok=True)
        proc_thread = Thread(
            target=self.processor.write_queue, args=(video_path, self.q)
        )
        proc_thread.start()
        self.queue_reader(output_path, read_interval=self.cfg.queue_wait)
        proc_thread.join()
        if self.cfg.print_stats:
            console.print(
                f"Stats for: {pretty_video_name}",
                f"\n\tTotal frames: {self.processor.stats['total']}",
                f"\n\tDecoded frames: {self.processor.stats['decoded']}",
                f"\n\tProduced frames: {self.processor.stats['produced']}",
                f"\n\tGated frames: {self.processor.stats['gated']}",
                style=f"bold {Color.magenta.value}",
            )

    def queue_reader(self, output_path, read_interval=0.1) -> None:
        """
        Reads frames from the queue and saves them as JPEG images.

        Args:
            output_path (str): The directory path where the frames will be saved.
            read_interval (float, optional): The time interval between reading frames from the queue.
                    Defaults to 0.1 seconds.
        """
        while True:
            if not self.q.empty():
                frame_object: FrameObject
                for frame_object in self.q.get():
                    if frame_object.metadata.get("end", False):
                        return
                    if frame_object.frame is not None and (
                        not self.devnull and isinstance(frame_object.frame, Image.Image)
                    ):
                        frame_object.frame.save(
                            os.path.join(
                                output_path,
                                f"{frame_object.metadata['frame_time']}.jpg",
                            )
                        )
            time.sleep(read_interval)
