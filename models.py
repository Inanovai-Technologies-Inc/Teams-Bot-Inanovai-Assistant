"""
The model menu.

One entry per model people can pick. Adding a model is one line here --
nothing else in the project changes.

`key`      what someone types to pick it, and what we store
`label`    what they see in the menu
`note`     one short line saying when to reach for it
`model`    the exact id the provider expects
`provider` which client talks to it: "hf" for our Hugging Face router,
           "org" for a model an organization brought with its own key
           (see orgs.py).
`thinks`   True when the model reasons before answering. Those need a
           larger token budget or the answer gets cut off before it starts.
"""

import pathlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Model:
    key: str
    label: str
    note: str
    model: str
    provider: str = "hf"
    thinks: bool = False


CATALOGUE = [
    Model("llama",    "Llama 3.3 70B",      "balanced, the safe default",
          "meta-llama/Llama-3.3-70B-Instruct"),
    Model("llama4",   "Llama 4 Maverick",   "newest Llama, fast",
          "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8"),
    Model("qwen",     "Qwen 2.5 72B",       "strong all-rounder",
          "Qwen/Qwen2.5-72B-Instruct"),
    Model("coder",    "Qwen 2.5 Coder 32B", "best for code questions",
          "Qwen/Qwen2.5-Coder-32B-Instruct"),
    Model("deepseek", "DeepSeek V3",        "good at long reasoning",
          "deepseek-ai/DeepSeek-V3"),
    Model("gptoss",   "GPT-OSS 20B",        "quick and cheap",
          "openai/gpt-oss-20b"),
    Model("gptoss120", "GPT-OSS 120B",      "the bigger GPT-OSS",
          "openai/gpt-oss-120b"),
    Model("glm",      "GLM 4.5 Air",        "thinks before answering",
          "zai-org/GLM-4.5-Air", thinks=True),
    Model("gemma",    "Gemma 3 12B",        "small and fast",
          "google/gemma-3-12b-it"),
    Model("command",  "Command A",          "good at writing and summaries",
          "CohereLabs/c4ai-command-a-03-2025"),
    Model("hermes",   "Hermes 3 70B",       "conversational, less formal",
          "NousResearch/Hermes-3-Llama-3.1-70B"),
]

BY_KEY = {m.key: m for m in CATALOGUE}
DEFAULT_KEY = "llama"


def org_catalogue(ais: list) -> list:
    """The menu for an organization that brought its own AI.

    `ais` are its saved AIs in order (each with .id, .label, .models). Models
    are listed by the exact name their provider uses; the first is the
    default. `provider` records which saved AI -- and so which key -- each
    model is reached through.
    """
    single = len(ais) == 1
    out, taken = [], set()
    for ai in ais:
        for name in ai.models:
            key = name.lower()
            if key in taken:
                # The same model saved under two keys: tell them apart.
                key = f"{ai.label.lower()}/{name.lower()}"
            taken.add(key)
            out.append(Model(
                key=key,
                label=name if single else f"{name} ({ai.label})",
                note="your organization's model" if single else f"your organization's {ai.label}",
                model=name, provider=f"org:{ai.id}"))
    return out


def get(key: str, catalogue: list | None = None) -> Model | None:
    """Look a model up by key, by menu number, or by a loose name match."""
    catalogue = CATALOGUE if catalogue is None else catalogue
    if not key:
        return None
    key = key.strip().lower()

    for m in catalogue:
        if key == m.key:
            return m

    if key.isdigit():
        i = int(key) - 1
        if 0 <= i < len(catalogue):
            return catalogue[i]
        return None

    # "llama 3.3", "qwen coder", "gpt oss" -- be forgiving about spacing
    squashed = key.replace(" ", "").replace("-", "").replace(".", "")
    for m in catalogue:
        if squashed == m.key or squashed in m.label.lower().replace(" ", "").replace(".", ""):
            return m
    return None


def menu(current_key: str, catalogue: list | None = None) -> str:
    """The numbered list people see when they type `model`."""
    catalogue = CATALOGUE if catalogue is None else catalogue
    lines = ["Pick a model - reply with a number or its name.", ""]
    for i, m in enumerate(catalogue, 1):
        mark = "   (using now)" if m.key == current_key else ""
        lines.append(f"{i}. {m.label} - {m.note}{mark}")
    lines.append("")
    lines.append("Your choice sticks to this chat until you change it.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command line
#
#     python models.py            list what is configured
#     python models.py --check    ask each model, report who answers
# ---------------------------------------------------------------------------

def _list():
    print()
    print(str(len(CATALOGUE)) + ' models configured. Default marked with *')
    print()
    print('      #  KEY         NAME                   MODEL ID')
    print('    ' + '-' * 88)
    for i, m in enumerate(CATALOGUE, 1):
        star = '*' if m.key == DEFAULT_KEY else ' '
        print('  %s %2d  %-10s  %-22s %s' % (star, i, m.key, m.label, m.model))
    print()
    print('    Run with --check to test which ones are answering right now.')
    print()


def _check():
    import asyncio, os, time
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=str(pathlib.Path(__file__).with_name('.env')),
                override=True)
    from openai import AsyncOpenAI

    token = os.environ.get('HF_TOKEN', '')
    if not token:
        print('No HF_TOKEN found in .env')
        raise SystemExit(1)

    client = AsyncOpenAI(api_key=token,
                         base_url='https://router.huggingface.co/v1')

    async def probe(m):
        started = time.time()
        try:
            reply = await asyncio.wait_for(
                client.chat.completions.create(
                    model=m.model,
                    max_tokens=600 if m.thinks else 200,
                    temperature=0,
                    messages=[{'role': 'user',
                               'content': 'Reply with one word: ready'}],
                ), timeout=120)
            text = (reply.choices[0].message.content or '').strip()
            status = 'OK' if text else 'EMPTY REPLY'
        except asyncio.TimeoutError:
            status = 'TIMEOUT'
        except Exception as error:
            detail = str(error)
            if '402' in detail or 'depleted' in detail.lower():
                status = 'NO CREDITS'
            else:
                status = 'FAIL ' + detail[:40]
        return m, status, time.time() - started

    async def main():
        print()
        print('Asking all ' + str(len(CATALOGUE)) + ' models to answer. One moment.')
        print()
        rows = await asyncio.gather(*(probe(m) for m in CATALOGUE))
        working = 0
        for m, status, secs in rows:
            ok = status == 'OK'
            working += ok
            mark = 'OK  ' if ok else '--  '
            trailer = '' if ok else status
            print('  %s %6.1fs  %-10s %-22s %s' % (mark, secs, m.key, m.label, trailer))
        print()
        print('  %d of %d answering right now.' % (working, len(CATALOGUE)))
        print()

    asyncio.run(main())


if __name__ == '__main__':
    import sys

    if '--check' in sys.argv:
        _check()
    else:
        _list()
