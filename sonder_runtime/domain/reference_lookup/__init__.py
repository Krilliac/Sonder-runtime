"""Pure reference-lookup logic: search terms, passage selection, prompt framing and answer checks.

Ported from the standalone lookup client with its measured behaviour unchanged.
Nothing here touches files, sqlite, the network, the environment or the clock;
the adapter reads the index and the caller supplies the random marker code.
"""
