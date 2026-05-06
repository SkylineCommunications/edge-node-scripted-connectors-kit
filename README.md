# Edge Node Scripted Connectors Kit

A collection of resources for building and working with DataMiner Edge Node scripted connectors.

---

## Repository Structure

### `examples/`
Ready-to-use scripted connector examples. Each example contains:
- **`src/`** — Raw source code of the scripted connector
- **`dist/`** — The packaged `.dmscript` file, ready to deploy

### `template/`
Contains a packaged scripted connector template (`ScriptedConnectorTemplate.zip`) to use as a starting point for building your own scripted connector.

### `wheels-resolver/`
A Python tool to work with Python wheels for scripted connectors. It can:
- **Check** whether wheels are available for a given package on Windows and Linux
- **Download** wheels for all supported platforms from a `requirements.txt` file

See [`wheels-resolver/README.md`](wheels-resolver/README.md) for usage details.
