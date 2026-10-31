# replay-interleaved-aac.mp4

Generated test material: twelve seconds of black 32 × 32 video at 60 fps and silent
48 kHz mono AAC. Contains no course content. It has 564 AAC samples interleaved
with video, enough to start four concurrent batches of 128 byte ranges.

The cancellation test adds padding to `mdat` at runtime and adjusts chunk offsets.
This keeps audio outside the initial index probe without storing a large fixture.
Metadata is at the end; leading metadata is covered by rearranging the original
two-second fixture and updating its chunk offsets in the test.

```bash
ffmpeg -f lavfi -i color=s=32x32:r=60 \
  -f lavfi -i anullsrc=r=48000:cl=mono \
  -t 12 -map 0:v:0 -map 1:a:0 \
  -c:v mpeg4 -c:a aac -map_metadata -1 replay-interleaved-aac.mp4
```
