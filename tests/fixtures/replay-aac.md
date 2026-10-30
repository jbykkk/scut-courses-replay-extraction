# replay-aac.mp4

Generated test material: two seconds of a black 32 × 32 frame and a 440 Hz sine wave,
MPEG-4 video + AAC audio. Contains no course content. The moov box is at the end,
so tests also cover progressive parsing without faststart metadata.

```bash
ffmpeg -f lavfi -i color=s=32x32:r=2 \
  -f lavfi -i sine=frequency=440:sample_rate=48000 \
  -t 2 -c:v mpeg4 -c:a aac replay-aac.mp4
```
