# Third-Party Notices

Nianlun builds on ideas and prior work from the following open-source projects.
Their names and copyrights remain the property of their respective owners.

## PageIndex

- Project: [VectifyAI/PageIndex](https://github.com/VectifyAI/PageIndex)
- Copyright: Copyright (c) 2025 Vectify AI
- License: MIT License
- Use in Nianlun: the structured document index tree and parts of the early
  indexing implementation originated from PageIndex and have since been
  refactored and extended independently.

The PageIndex copyright notice and MIT License are retained in the root
[`LICENSE`](LICENSE) file.

## DeerFlow

- Project: [bytedance/deer-flow](https://github.com/bytedance/deer-flow)
- Copyright: Copyright (c) 2025 Bytedance Ltd. and/or its affiliates
- Copyright: Copyright (c) 2025-2026 DeerFlow Authors
- License: MIT License
- Use in Nianlun: parts of the Agent implementation contain a small amount of
  code adapted from DeerFlow. Its middleware composition, context management,
  clarification flow, and sub-agent isolation design also informed Nianlun's
  architecture. The adapted code has been modified for Nianlun and continues
  to be covered by DeerFlow's upstream copyright and license terms.

The DeerFlow copyright notices and MIT License are retained in the root
[`LICENSE`](LICENSE) file. Nianlun does not incorporate the complete DeerFlow
platform or general-purpose sub-agent executor.

## Python And JavaScript Dependencies

Third-party packages installed through Python or JavaScript package managers
remain subject to their own licenses. Refer to `pyproject.toml`, `uv.lock`, and
`app/frontend/package-lock.json` for the dependency inventory.
