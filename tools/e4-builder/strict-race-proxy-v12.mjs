#!/usr/bin/env node
// Backward-compatible V12 entrypoint.
// Strict output protection now runs inside race-proxy-v3 itself, eliminating
// an entire Node child-process + stdin/stdout JSON hop from the hot path.
import "./race-proxy-v3.mjs";
