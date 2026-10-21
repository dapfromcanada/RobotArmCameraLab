# Temporal noise vs gain, 2026-10-21

Temporal σ = std of (frame A − frame B)/√2 on the grey patch: the print texture and
light gradient cancel, leaving sensor noise. Spatial σ is the same patch in one frame.


## triton: temporal noise vs gain (wedge white ≈ 0.5)

| point | exposure µs | gain dB | fps | wedge white | grey level | grey spatial σ (print+light) | grey temporal σ | temporal SNR |
|---|---|---|---|---|---|---|---|---|
| gain 0 dB | 377618 | 0.0 | 2.6 | 0.571 | 0.236 | 0.0154 | 0.00299 | 79 |
| gain 6 dB | 189249 | 6.0 | 2.6 | 0.571 | 0.236 | 0.0107 | 0.00288 | 82 |
| gain 12 dB | 94860 | 12.0 | 2.6 | 0.571 | 0.236 | 0.0108 | 0.00383 | 62 |
| gain 18 dB | 47544 | 18.0 | 20.8 | 0.572 | 0.236 | 0.0117 | 0.00570 | 41 |
| gain 24 dB | 23836 | 24.0 | 20.8 | 0.567 | 0.240 | 0.0090 | 0.00518 | 46 |
| gain 28.6 dB | 14023 | 28.6 | 20.8 | 0.566 | 0.245 | 0.0108 | 0.00251 | 97 |
| gain 36 dB | 5978 | 36.0 | 20.8 | 0.565 | 0.243 | 0.0112 | 0.00349 | 70 |
| gain 36 dB, 4-frame average | 5978 | 36.0 | 20.8 | 0.567 | 0.243 | 0.0106 | 0.00172 | 141 |

## apex: temporal noise vs gain (wedge white ≈ 0.5)

| point | exposure µs | gain dB | fps | wedge white | grey level | grey spatial σ (print+light) | grey temporal σ | temporal SNR |
|---|---|---|---|---|---|---|---|---|
| gain 0 dB | 8557 | 0.0 | 12.0 | 0.511 | 0.109 | 0.0080 | 0.00488 | 22 |
| gain 6 dB | 4288 | 6.0 | 12.0 | 0.504 | 0.108 | 0.0093 | 0.00676 | 16 |
| gain 12 dB | 2149 | 12.0 | 12.0 | 0.503 | 0.108 | 0.0116 | 0.00957 | 11 |
| gain 18 dB | 1077 | 18.0 | 12.0 | 0.505 | 0.109 | 0.0155 | 0.01367 | 8 |
| gain 18 dB, 4-frame average | 1077 | 18.0 | 12.0 | 0.514 | 0.111 | 0.0102 | 0.00697 | 16 |
