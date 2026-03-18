# Mnemo: Memory for AI Agents

## What Is Mnemo?

Mnemo gives AI agents the ability to remember things — persistently, across conversations, and over time.

Today, most AI agents start every conversation from scratch. They don't remember what happened yesterday, what they learned last week, or what worked (or didn't) on a previous task. Mnemo changes that. It's a memory server that an AI agent can talk to, storing and retrieving knowledge in natural language.

The key design idea is **simplicity for the agent, sophistication behind the scenes**. An agent doesn't need to organize, label, or structure its memories. It simply says what happened, and Mnemo takes care of the rest — breaking that text into meaningful pieces, classifying them, tracking how confident the memory is, building connections between related memories, and making everything searchable later.

---

## How It Works (In Plain Terms)

### Storing Memories

When an agent wants to remember something, it sends a plain-language message to Mnemo. For example:

> "I discovered that the CSV file had corrupted data in row 847. Pandas was silently converting text to numbers. I should always specify column types explicitly when loading CSV files."

Mnemo automatically breaks this into three separate memories:

1. **An experience** — "I discovered corrupted data in row 847" (a specific thing that happened)
2. **A fact** — "Pandas silently converts mixed-type columns" (a general piece of knowledge)
3. **A lesson** — "Always specify column types when loading CSVs" (a rule for future behavior)

Each type of memory is treated differently. Experiences fade quickly (they're about specific moments), facts stick around longer, and lessons persist the longest — because good practices remain valuable over time.

Mnemo also notices when the agent stores something it already knows. Instead of creating a duplicate, it increases its confidence in the existing memory. Telling the system the same thing twice makes it *more certain*, not redundant.

### Retrieving Memories

When an agent needs to recall something, it asks a question in natural language:

> "What do I know about loading CSV files?"

Mnemo searches its memory using the meaning of the question (not just keyword matching), finds the most relevant memories, and returns them ranked by a combination of relevance and confidence. It can also follow connections in its knowledge graph to surface related memories the agent didn't directly ask about.

### Memory Decay

Just like human memory, Mnemo's memories fade over time. A specific experience from two weeks ago becomes less prominent, while a well-established lesson persists. But memories that are frequently accessed stay fresh — just as revisiting a memory in real life keeps it vivid.

A background process periodically reviews all memories: fading ones are retired, similar memories are consolidated into broader knowledge, and duplicates are merged. This keeps the memory store clean and relevant without any manual maintenance.

---

## Connecting Memories Together

Mnemo doesn't just store isolated facts — it builds a web of connections between them. When an agent stores related information together, Mnemo automatically links the pieces:

- An experience ("I found a bug") connects to the fact it revealed ("This library has a known issue")
- A lesson ("Always check for this") connects to the fact that motivated it

When retrieving memories later, Mnemo can follow these connections to surface related context that the agent didn't explicitly ask for — like remembering not just a fact, but *why* you know it and *what to do about it*.

---

## Sharing Knowledge Between Agents

Agents can share what they've learned with each other through **views** — curated snapshots of their knowledge.

For example, an agent that has become expert in data processing can create a view containing all its procedural knowledge about CSV handling, then share it with another agent that's just starting a similar task. The receiving agent can search through this shared knowledge just like its own memories.

Sharing is controlled through a permissions system:
- The sharing agent decides exactly what to share and with whom
- Shared views are frozen at the time of creation — later changes to the original agent's memories don't affect what was shared
- Permissions can be revoked at any time
- When an agent leaves the system, all the access it granted is automatically revoked

### Skill Export

A special form of sharing is **skill export** — packaging an agent's procedural knowledge (lessons, rules, best practices) along with the supporting facts into a structured document. This is like creating a training manual from one agent's experience that another agent can learn from.

---

## How Agents and Applications Connect to Mnemo

Mnemo provides three ways for agents and applications to interact with it:

### 1. The REST API

The most direct way to use Mnemo. Applications send HTTP requests to endpoints like:

- **Remember** — Send text to be stored as memories
- **Recall** — Search for relevant memories by asking a question
- **Stats** — See how many memories an agent has and their health
- **Share** — Create and share knowledge views with other agents
- **Depart** — Gracefully remove an agent and clean up its shared access

The API handles authentication (optional), validates requests, and returns structured responses. It's suitable for any programming language or platform that can make web requests.

### 2. The Python Client Library

For Python applications, Mnemo provides a ready-made client library (`mnemo-ai`) that wraps the API into simple function calls. Instead of constructing HTTP requests manually, developers can write:

```python
from mnemo.client import MnemoClient

async with MnemoClient(base_url="http://localhost:8000") as client:
    # Store a memory
    await client.remember(agent_id, "The deployment failed because of a missing config file")

    # Search memories
    results = await client.recall(agent_id, "deployment issues")
```

A synchronous version is also available for simpler scripts that don't need async support.

### 3. The MCP Server (for AI Assistants like Claude)

The **Model Context Protocol (MCP)** server allows AI assistants that support MCP (such as Claude) to use Mnemo directly as a tool during conversations. When configured, the AI assistant gains access to these tools:

- **mnemo_remember** — Store something the assistant has learned
- **mnemo_recall** — Search the assistant's memories for relevant information
- **mnemo_stats** — Check on the state of the assistant's memory
- **mnemo_share** — Share a collection of memories with another agent
- **mnemo_list_shared** — See what's been shared (both incoming and outgoing)
- **mnemo_recall_shared** — Search through memories that others have shared
- **mnemo_revoke_share** — Take back a previously shared collection

This means an AI assistant can remember things across conversations, build up expertise over time, and even share what it's learned with other AI agents — all through natural tool use during regular conversations.

---

## Security and Trust

- **Authentication** can be enabled so that only authorized applications can access the server
- **Agents are isolated** — one agent cannot access another's memories unless explicitly shared
- **Shared views are scope-bounded** — when searching through shared knowledge, it's impossible to accidentally access memories outside what was intentionally shared
- **Retrieved memories are clearly labeled** as reference material, not instructions — this prevents memory content from being confused with system commands
- **Confidence levels** are shown alongside memories (high, moderate, low) so consumers can gauge how reliable each piece of knowledge is

---

## What Mnemo Is Not

- **Not a database replacement.** Mnemo is purpose-built for AI agent memory — semantic search, decay dynamics, confidence tracking, and knowledge sharing. It's not a general-purpose data store.
- **Not a chat history.** Mnemo doesn't record conversations verbatim. It extracts and organizes the *knowledge* from what agents tell it.
- **Not a training system.** Mnemo doesn't modify AI model weights. It provides external, retrievable memory that agents can consult during their work.

---

## Summary

Mnemo turns forgetful AI agents into agents that learn and remember. It accepts plain language, organizes knowledge automatically, lets memories naturally fade or strengthen over time, connects related information into a knowledge graph, and enables agents to share what they've learned. Whether accessed through the REST API, a Python library, or as an MCP tool inside an AI assistant, Mnemo provides the persistent memory layer that AI agents need to grow more capable over time.
