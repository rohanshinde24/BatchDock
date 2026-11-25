"""Deterministic synthetic field extraction, with independently parsed ground truth."""
import json
import random
import re

FIELDS = ('replacement_sku', 'transfer_sku', 'approved_sku', 'returned_sku')


def generate():
    records = []
    for i in range(200):
        field = FIELDS[i % 4]
        target = f"BD-{71000 + i * 13}"
        lines = [f'{field}: {target}', f'previous_sku: OLD-{42000 + i * 7}',
                 f'alternative_sku: ALT-{13000 + i * 11}', f'reference: CASE-{30000 + i}',
                 f'quantity: {i % 9 + 1}', f'department: {("engineering", "facilities", "operations", "support")[i % 4]}',
                 'note: The previous and alternative items are reference information, not the selected item.']
        band = ('short', 'medium', 'long')[(i // 4) % 3]
        for j in range({'short': 0, 'medium': 4, 'long': 12}[band]):
            lines.append(f'History {j + 1}: ticket REF-{i * 19 + j + 1000} was reviewed by team {(i + j) % 7}; '
                         'the review concerned packaging and delivery only and did not change the selected SKU.')
        random.Random(1009 + i).shuffle(lines)
        prompt = (f'Read this operations record. Return only the exact value of {field}, '
                  'with no explanation or punctuation. Ignore other SKU fields.\n\n' + '\n'.join(lines))
        records.append({'id': f'extract-{i:03d}', 'prompt': prompt, 'expected': target,
                        'field': field, 'length_band': band})
    random.Random(4242).shuffle(records)
    return records


def oracle(record):
    if record['field'] not in FIELDS:
        raise ValueError('Unknown extraction family')
    values = re.findall(r'^' + re.escape(record['field']) + r': (BD-\d+)$', record['prompt'], re.M)
    if len(values) != 1 or record['expected'] != values[0]:
        raise ValueError('Ground truth does not match unique source field')
    return values[0]


def load(path):
    records = [json.loads(line) for line in path.read_text().splitlines()]
    if len(records) != 200 or len({r['id'] for r in records}) != 200 or len({r['prompt'] for r in records}) != 200:
        raise ValueError('Require 200 distinct IDs and prompts')
    for r in records:
        oracle(r)
    return records


if __name__ == '__main__':
    print('\n'.join(json.dumps(r, separators=(',', ':')) for r in generate()))
