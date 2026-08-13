# Luna/Terra Assessment Calibration Report

**Date:** YYYY-MM-DD
**Reviewer:** (name)
**Corpus version:** (manifest hash)
**Mode:** smoke (5 sessions) / full (30+ sessions)

## 1. Classification accuracy

| Class | Precision | Recall | F1 | Support |
|-------|-----------|--------|----|---------|
| progressing | | | | |
| stalled | | | | |
| looping | | | | |
| off_scope | | | | |
| terminal_completed | | | | |
| terminal_failed | | | | |
| idle | | | | |

- **Macro-F1:** 
- **Macro-Precision:** 
- **Macro-Recall:** 

## 2. Attention metrics

- **Precision:** (of all sessions flagged needs_attention, how many actually needed it)
- **Recall:** (of all sessions that actually needed attention, how many were flagged)
- **False-attention rate:** (fraction of flagged sessions that did not need attention)

## 3. Citation validity

- **Total assessments:** 
- **Citations valid:** 
- **Citations invalid:** 
- **Validity rate:** 

Every assessment evidence ref_id must resolve to a packet event, signal, or
system_refs ID. Invalid citations indicate the model is hallucinating
references.

## 4. Latency

- **Min / Max / Mean (ms):** 
- **P50 / P95 (ms):** 

## 5. Token usage

- **Total input / output tokens:** 
- **Mean input / output per call:** 
- **Call count:** 

## 6. Ambiguous cases

- **Count:** 
- **Threshold:** 30

If the ambiguous-case count is below 30, no percentage-point verdict is
emitted (spec 11). Record this explicitly.

## 7. Cascade decision (keep or remove)

- **Terra improvement on ambiguous cases:** yes / no
- **Decision:** keep / disable automatic escalation
- **Rationale:** 
- **Reviewer sign-off:** 

## 8. Release gate status

- [ ] Macro-F1 meets threshold
- [ ] Attention precision meets threshold
- [ ] Citation validity rate = 1.0
- [ ] Latency within budget
- [ ] Token cost within daily ceiling
- [ ] Ambiguous-case count >= 30
- [ ] Cascade decision recorded with rationale
