"""Regression coverage for recovery, publication and malformed persisted data."""

import importlib.util
import json
import sys
import unittest
from unittest import mock

from test_session_tools import SCRIPTS, Sandbox, sc
import vault_common


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), SCRIPTS / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sync = load_script('progress-sync')
importer = load_script('nblm-import')


class TestReviewRegressions(Sandbox):
    def test_summary_names_do_not_merge_distinct_sessions(self):
        for slug in ('a b', 'a-b', 'A-B', '日本語', '中文'):
            self.init_route_b(slug)
            self.state('set', *self.v(slug), '--status', 'complete')
        self.sync('push', '--vault', str(self.vault))
        meta = json.loads((sc.progress_dir(self.vault) / 'notebook.json').read_text())
        notebook = json.loads(self.nblm_db.read_text())['notebooks'][meta['notebook_id']]
        for slug, source in meta['session_sources'].items():
            self.assertIn(f'# Session {slug} —', notebook['sources'][source]['text'])

    def test_pull_rediscovers_a_source_replaced_by_another_machine(self):
        self.init_route_b()
        self.sync('push', '--vault', str(self.vault))
        db = json.loads(self.nblm_db.read_text())
        notebook = next(iter(db['notebooks'].values()))
        source = notebook['sources'].pop(next(iter(notebook['sources'])))
        source['text'] = 'snapshot from another machine'
        notebook['sources']['replacement'] = source
        self.nblm_db.write_text(json.dumps(db))
        self.sync('pull', '--vault', str(self.vault))
        self.assertEqual((sc.progress_dir(self.vault) / 'state.pulled.md').read_text(), source['text'])

    def test_pull_explicit_notebook_ignores_cached_source(self):
        self.init_route_b()
        self.sync('push', '--vault', str(self.vault))
        db = json.loads(self.nblm_db.read_text())
        db['notebooks']['other'] = {'title': 'Other', 'sources': {
            'other-source': {'title': 'duel-vault-state-other.md', 'text': 'other snapshot'}}}
        self.nblm_db.write_text(json.dumps(db))
        self.sync('pull', '--vault', str(self.vault), '--notebook', 'other')
        self.assertEqual((sc.progress_dir(self.vault) / 'state.pulled.md').read_text(), 'other snapshot')

    def test_restore_validates_entire_snapshot_before_writing(self):
        valid = sc.new_state(self.vault, 'restored', 'B', 'Vault', 'Title', 'Task')
        invalid = [None, 'bad', {'session': '../outside'},
                   {**valid, 'session': 'invalid', 'status': 'complete'},
                   {**valid, 'session': 'invalid', 'pending': [None]}]
        for item in invalid:
            with self.subTest(item=item):
                text = sync.STATE_MARKER + '\n' + json.dumps({
                    'schema': sc.SCHEMA, 'open_sessions': [valid, item]})
                self.assertEqual(sync.restore(self.vault, text), sc.EXIT_FAIL)
                self.assertFalse(sc.session_dir(self.vault, 'restored').exists())

    def test_verify_reports_malformed_token_entries(self):
        self.init_route_b()
        for entry in (None, 42, 'bad', {'resource': [], 'total': 1}):
            with self.subTest(entry=entry):
                sc.write_json_atomic(sc.tokens_path(self.vault, 's1'), {'schema': 1, 'entries': [entry]})
                result = self.log('verify', *self.v(), '--json', expect=1)
                self.assertNotIn('Traceback', result.stderr)
                self.assertIn('SL009', [i['code'] for i in json.loads(result.stdout)['issues']])


class TestFrontmatterRoundtrip(unittest.TestCase):
    def test_imported_strings_roundtrip_with_and_without_pyyaml(self):
        values = {'project': 'true', 'session': '001', 'path': 'C:\\notes\\file',
                  'title': 'first\nsecond "quoted"', 'tags': ['a,b', 'yes', 'x"y']}
        text = importer.dump_frontmatter(values)
        for parser in (vault_common.yaml, None):
            with self.subTest(parser=parser), mock.patch.object(vault_common, 'yaml', parser):
                data, error, _line = vault_common.parse_frontmatter(text)
                self.assertIsNone(error)
                self.assertEqual(data, values)


if __name__ == '__main__':
    unittest.main()
