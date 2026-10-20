# Replay study, phase 1: reverted agent PRs

| Agent | Sampled merged PRs (Python, 2026) | Reverted | Rate |
|---|---|---|---|
| copilot-swe-agent | 270 | 1 | 0.4% |
| google-labs-jules | 270 | 0 | 0.0% |
| devin-ai-integration | 270 | 0 | 0.0% |
| claude | 270 | 0 | 0.0% |
| cursor | 248 | 0 | 0.0% |

Reverts are found by the GitHub revert button's text (`Reverts <repo>#<n>`) or a
`Revert "<title>"` title, so this undercounts manual reverts and later fixes.

## Reverted PRs

- [danmartinez78/ros2-VLM-demo#25](https://github.com/danmartinez78/ros2-VLM-demo/pull/25) (copilot-swe-agent): Add repeatable latency, throughput, and resource benchmarks → reverted by [#26](https://github.com/danmartinez78/ros2-VLM-demo/pull/26)
