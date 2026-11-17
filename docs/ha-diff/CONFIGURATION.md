# Raw configuration references

Changed rows include references to their recorded step configuration and a label
from the deepest available alias, service, action, condition or trigger field.
Each label has its own raw-field reference. The source configuration is never
rewritten or converted to a normalized YAML representation.

The path adapter follows the rules in Home Assistant frontend commit
`5253eef284e3bcb336c721821ec7751bb3bde3ee`:

- [data/trace.ts](https://github.com/home-assistant/frontend/blob/5253eef284e3bcb336c721821ec7751bb3bde3ee/src/data/trace.ts): singular trace roots map to legacy singular or modern plural config fields; singleton index 0 and implicit sequence wrappers need no raw array segment; `if/condition/N` maps to the conditions directly inside `if`.
- [data/automation.ts](https://github.com/home-assistant/frontend/blob/5253eef284e3bcb336c721821ec7751bb3bde3ee/src/data/automation.ts): nested `triggers` groups flatten to trigger ordinals. The Python adapter retains all original group/list pointer segments while flattening.

Script `sequence`, choose, repeat, parallel and entity-specific condition paths
use their existing raw fields. A manual `trigger` step is never falsely linked
to configured trigger 0. Unsupported paths return the deepest resolved raw
reference plus an explicit unresolved suffix; they are labelled `partial`.
Configuration matching is explanatory and does not establish execution or
evaluate conditions. It never changes which recorded values are compared.

Tests cover both unmodified legacy/singleton upstream configuration and modern
controlled variants. JSON Pointer escaping/resolution comes from jsonpointer,
not a custom pointer implementation. No frontend implementation file is bundled.

