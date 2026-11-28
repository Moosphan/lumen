#!/usr/bin/env python3
"""Receive opt-in Codex and Gemini CLI hooks as reviewable observations."""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lumen.service import dispatch


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument('agent', choices=('Codex', 'Gemini CLI'))
    agent = cli.parse_args().agent
    try:
        budget = 2 * 1024 * 1024
        payload = sys.stdin.buffer.read(budget + 1)
        if len(payload) > budget:
            raise ValueError('事件超过读取上限')
        state = Path(os.environ.get('LUMEN_STATE_DIR') or
                     (Path.home() / 'Library/Application Support/org.lumen.cleaner'))
        dispatch(state, {'cmd': 'agent_event', 'agent': agent, 'event': json.loads(payload)})
    except (OSError, ValueError, KeyError, RecursionError, RuntimeError) as error:
        print(f'Lumen: {error}', file=sys.stderr)
    # Hooks must not block the Agent or inject instructions into its session.
    print('{}')


if __name__ == '__main__':
    main()
