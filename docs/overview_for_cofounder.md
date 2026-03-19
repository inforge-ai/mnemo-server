# Mnemo — What We've Built and Why

*For a non-engineering reader. Written to inform a conversation about what to build next.*

---

## The Core Idea

Mnemo is a **memory server for AI agents**. It gives an AI agent — Claude, or any other —
a place to remember things between conversations, across sessions, and across contexts.
Without something like Mnemo, every conversation an AI agent has starts from zero. It knows
what it was trained on, but nothing about *you*, nothing about *this project*, nothing about
what it figured out last Tuesday.

Mnemo changes that. An agent using Mnemo can say "I remember that" — and mean it.

The name comes from Mnemosyne, the Greek goddess of memory and the mother of the Muses. The
idea that memory is the precondition for creativity and learning is old. We're making it
literal.

---

## The Philosophical Backbone

The design rests on a taxonomy of memory types that will be familiar from philosophy of mind
and cognitive science:

**Episodic memory** — what happened to me, when, in what context.
> *"I found a bug in the authentication service while testing the login flow yesterday."*

**Semantic memory** — what is true about the world, stated as general fact.
> *"asyncpg does not auto-commit database transactions."*

**Procedural memory** — what one should do; rules, habits, lessons encoded as action.
> *"Always specify dtype explicitly when loading CSV files."*

When an agent tells Mnemo something in plain English, Mnemo reads it — sentence by
sentence — and decides which kind of memory each sentence is. It does this automatically,
without asking the agent to label or structure anything. The agent just says what happened.

This matters because these types of memory behave differently over time, carry different
epistemic weight, and serve different purposes in reasoning. An episodic memory ("I saw
this happen") is strong evidence but fades fastest. A procedural rule ("always do X") is
a compressed lesson from many episodes. Semantic facts sit between — general truths with
moderate confidence and slow decay.

---

## Confidence as a First-Class Concept

Mnemo does not store memories as true or false. It stores them as **degrees of belief**,
using a well-established probabilistic framework (the Beta distribution). Every memory has
a confidence level — and that confidence is *inferred from how the agent speaks*, not
declared.

- Phrases like *"I confirmed"*, *"I verified"*, *"definitely"* → high confidence
- Default statements of fact or procedure → moderate confidence
- Phrases like *"I think"*, *"maybe"*, *"it could be"* → low confidence

This is closer to how belief actually works than any binary system. A memory is not a
locked drawer; it is an assessment that updates with evidence.

---

## Memories Decay

Mnemo takes seriously the idea that relevance fades. Every memory has a **half-life** —
the time in which its effective confidence halves. Episodic memories fade fastest (two
weeks). Semantic facts persist longer (three months). Procedural rules are the most
durable (six months).

But decay is not just time-based. Memory that is *used* — recalled and applied — fades
more slowly. Access refreshes relevance. This mirrors what we know about human memory
consolidation: memories we revisit persist; memories we ignore dissolve.

A background process runs every hour and deactivates memories whose effective confidence
has dropped below a threshold. They are not deleted — the record remains — but they no
longer surface in retrieval.

---

## The Knowledge Graph

Memories are not stored in isolation. When an agent submits a piece of text containing
multiple sentences, Mnemo infers the relationships between them and creates **edges**:

- *"I saw the service crash under load"* → **evidence for** → *"Services need backpressure"*
- *"Always add rate limiting to public APIs"* → **motivated by** → *"Unconstrained APIs fail under load"*

Over time, each agent builds a knowledge graph — a network of memories connected by
meaningful relationships. When an agent searches for something, Mnemo doesn't just return
the closest match. It also traverses the graph from that match, surfacing related memories
the agent might not have known to ask for.

---

## Consolidation: The Nightly Work

Once an hour, Mnemo runs a process called consolidation. This is modelled on what happens
in the brain during sleep — the reorganisation of experience into more durable, general
knowledge.

**What consolidation does:**

1. **Fades** memories whose effective confidence has dropped below threshold — they stop
   appearing in search results.

2. **Generalises** clusters of similar episodic memories into a new semantic memory. If an
   agent has logged three separate encounters with the same kind of problem, Mnemo creates
   a semantic atom: *"I have encountered this pattern repeatedly"* — and links it back to
   the episodes that generated it.

3. **Merges** near-duplicate memories. If the agent stored the same fact twice (once as
   *"asyncpg doesn't auto-commit"* and once as *"asyncpg requires explicit commits"*),
   consolidation recognises these as the same belief and merges them, combining their
   confidence rather than inflating the apparent evidence.

4. **Prunes** dead edges — connections in the knowledge graph that now point to deactivated
   memories.

5. **Removes** data belonging to agents who have left the system and whose retention period
   has expired.

The whole process is safe: each step runs in an isolated transaction, and a distributed
lock prevents two consolidation runs from interfering with each other.

---

## Sharing Knowledge: Views and Skills

An agent can package a portion of its memory as a **view** — a named, filtered snapshot of
its knowledge. For example, an agent that has accumulated months of experience with Python
data pipelines might package its procedural rules into a view called *"pandas-best-practices"*.

This view can be **shared** with another agent. The receiving agent can search through it,
traverse its internal connections, and learn from it — but they cannot see the grantor's
full memory, only what was included in the snapshot.

When an agent departs the system, all views it shared are automatically revoked. The sharing
relationship ends with the sharer. This is a hard rule, not a setting.

A view can also be **exported as a skill** — a structured markdown document containing the
procedural rules and supporting facts from the snapshot. This can be injected into another
agent's context directly, or stored for later use.

---

## How an Agent Talks to Mnemo

An agent using Mnemo has access to three primary actions:

**Remember** — *"Store this for me."*
The agent submits free-form text. Mnemo decomposes it, classifies it, generates a semantic
embedding, checks for near-duplicates, links it to related existing memories, and stores it.
The agent does not need to know any of this is happening.

**Recall** — *"What do I know about X?"*
The agent asks a question in natural language. Mnemo finds the most relevant memories using
semantic similarity (not keyword matching), filters by confidence, and traverses the
knowledge graph to surface related context. Access updates the memories' decay timers.

**Stats** — *"How much do I know, and how confidently?"*
A summary of the agent's memory state: how many memories are active, broken down by type,
with average confidence and graph density.

---

## What Is Working Now

As of this version, the following is fully built and tested (104 automated tests, all passing):

| Capability | Status |
|---|---|
| Memory storage from free text | Complete |
| Automatic classification (episodic / semantic / procedural) | Complete |
| Confidence inference from linguistic cues | Complete |
| Semantic search with embedding similarity | Complete |
| Knowledge graph with typed edges | Complete |
| Graph traversal during recall | Complete |
| Memory decay over time | Complete |
| Hourly consolidation (fade, generalise, merge, prune) | Complete |
| Duplicate detection and Bayesian merging | Complete |
| Views (named memory snapshots) | Complete |
| Cross-agent knowledge sharing via views | Complete |
| Access revocation on departure | Complete |
| Skill export to markdown | Complete |
| REST API (full) | Complete |
| Python client library | Complete |
| MCP server (Claude Desktop / claude.ai integration) | Complete — 3 tools: remember, recall, stats |

---

## What Is Not Yet Built

| Capability | Notes |
|---|---|
| Skill files (Claude + OpenClaw guides) | Markdown documents telling agents *how* to use Mnemo; last remaining deliverable |
| Authentication / API keys | Not in scope for v0.1; single-user deployment assumed |
| Contradiction detection | Deliberately deferred; duplicate merging covers the common case |
| Live subscriptions to shared views | Snapshots only in v0.1; deliberate simplification |

---

## Design Decisions Worth Knowing

**The agent never labels its own memories.** The act of classification is the server's
responsibility. An agent that has to stop and decide *"is this episodic or semantic?"*
before it can remember something has been given too much to think about. Just say what
happened.

**Confidence is read from language, not set by the agent.** The agent's epistemic state is
expressed in the words it chooses. Mnemo reads those words. This means the agent's natural
hedging and certainty language directly affects how strongly its memories persist and
surface in recall.

**Decay is not a feature — it is a constraint.** Memories that are never recalled, never
refreshed, never connected to new experience, should fade. A system that accumulates
everything forever is not memory; it is a log. The distinction matters.

**Sharing is scoped and revocable.** Knowledge shared between agents is always bounded by
what the grantor chose to include and always ends when the grantor leaves. There is no
ambient, unattributed knowledge. Every memory has a provenance.

---

*This document was written to give a co-founder context for a conversation about what to
build next. If you are reading this in Claude Desktop: the system described above is
working and testable. The author would welcome a conversation about which aspect of memory,
sharing, or consolidation behaviour you think is most important to develop further.*
