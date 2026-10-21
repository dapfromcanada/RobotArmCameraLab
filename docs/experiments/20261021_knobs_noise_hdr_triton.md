# Bench experiments 2026-10-21: knobs, noise, HDR, averaging

Charts under the cameras: step wedge, grey patch, resolution, tag card (Letter prints).
Levels are 0–1 of full scale; SNR = grey level / spatial σ on the grey patch.

## A TRI054S HDR off: gain vs noise (white ≈ 0.5)

| point | exposure µs | gain dB | fps | wedge white | steps resolved | clipped steps | grey level | grey noise σ | grey SNR | MTF50 cyc/px |
|---|---|---|---|---|---|---|---|---|---|---|
| gain 28.6 dB | 18066 | 28.6 | 20.8 | 0.644 | 15 | 0 | 0.278 | 0.0120 | 23.1 | 0.119 |
| gain 24 dB | 30682 | 24.0 | 20.8 | 0.643 | 14 | 0 | 0.267 | 0.0144 | 18.5 | 0.125 |
| gain 18 dB | 61237 | 18.0 | 16.2 | 0.648 | 14 | 0 | 0.268 | 0.0129 | 20.9 | 0.116 |
| gain 12 dB | 122165 | 12.0 | 8.2 | 0.648 | 15 | 0 | 0.268 | 0.0122 | 22.0 | 0.123 |
| gain 6 dB | 243756 | 6.0 | 4.1 | 0.648 | 13 | 0 | 0.268 | 0.0138 | 19.4 | 0.126 |

## D TRI054S averaging (gain 28.6 dB)

| point | exposure µs | gain dB | fps | wedge white | steps resolved | clipped steps | grey level | grey noise σ | grey SNR | MTF50 cyc/px |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 frame | 18066 | 28.6 | 20.8 | 0.644 | 15 | 0 | 0.278 | 0.0120 | 23.2 | 0.117 |
| 4 frames averaged | 18066 | 28.6 | 20.8 | 0.641 | 15 | 0 | 0.276 | 0.0118 | 23.5 | 0.128 |

## B TRI054S HDR on, fixed gains 19.8/4.2/29.4/4.2 dB: channel exposure sweep

| point | exposure µs | gain dB | fps | wedge white | steps resolved | clipped steps | grey level | grey noise σ | grey SNR | MTF50 cyc/px |
|---|---|---|---|---|---|---|---|---|---|---|
| ch 5 ms | 5003 | - | 20.8 | 0.337 | 15 | 0 | 0.144 | 0.0068 | 21.2 | 0.126 |
| ch 10 ms | 10000 | - | 20.8 | 0.479 | 15 | 0 | 0.206 | 0.0092 | 22.4 | 0.112 |
| ch 20 ms | 19996 | - | 20.8 | 0.677 | 15 | 0 | 0.292 | 0.0119 | 24.4 | 0.122 |
| ch 30 ms | 29991 | - | 20.8 | 0.830 | 14 | 0 | 0.344 | 0.0185 | 18.6 | 0.131 |
| ch 40 ms | 40007 | - | 20.8 | 0.964 | 14 | 0 | 0.399 | 0.0203 | 19.7 | 0.132 |
| ch 47.9 ms | 47910 | - | 20.8 | 1.000 | 12 | 2 | 0.437 | 0.0215 | 20.4 | 0.130 |
