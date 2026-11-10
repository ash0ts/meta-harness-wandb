# SPDX-License-Identifier: MIT
import unittest
from unittest.mock import patch

from mhforge.telemetry import adapter_wandb as adapter
from tests.test_adapters import AdapterCase


class TableReadback(AdapterCase):
    def test_stored_cells_must_equal_plan_including_types_and_order(self):
        sink = self.wandb_sink()
        table = {'key': 'scores', 'columns': ['index', 'correct'], 'rows': [[0, True], [1, False]]}
        stored = {'columns': table['columns'], 'data': table['rows']}
        with patch.object(sink, '_api', return_value=object()), \
             patch.object(adapter, '_sdk_run_summary', return_value={'scores': {'_type': 'table-file'}}), \
             patch.object(adapter, '_sdk_table_data', return_value=stored) as read:
            self.assertEqual(sink.confirm_table_contents('e/p/r', [table]), [])
            for changed in ({'columns': ['correct', 'index'], 'data': table['rows']},
                            {'columns': table['columns'], 'data': list(reversed(table['rows']))},
                            {'columns': table['columns'], 'data': [[0, 1], [1, False]]},
                            {'columns': table['columns'], 'data': [[0, True]]}):
                read.return_value = changed
                self.assertEqual(sink.confirm_table_contents('e/p/r', [table]),
                                 ['table_content_mismatch:scores'])

    def test_read_failures_and_ces_markers_never_verify_cells(self):
        sink = self.wandb_sink()
        table = {'key': 'scores', 'columns': [], 'rows': []}
        with patch.object(sink, '_api', return_value=object()), \
             patch.object(adapter, '_sdk_run_summary', return_value={'scores': {'_type': 'table-file'}}) as summary, \
             patch.object(adapter, '_sdk_table_data', side_effect=OSError):
            self.assertEqual(sink.confirm_table_contents('e/p/r', [table]),
                             ['table_content_read_failed:scores:OSError'])
            summary.return_value = {'scores': {'_type': 'eval-table-ces'}}
            self.assertEqual(sink.confirm_table_contents('e/p/r', [table]),
                             ['table_content_unavailable:scores'])

    def test_table_download_rejects_arbitrary_paths(self):
        for path in ('../../secret', 'media/table/../../../secret.table.json', '/tmp/x.table.json', None):
            with self.assertRaises(adapter.ExportError):
                adapter._sdk_table_data(object(), 'e/p/r', {'path': path}, self.st.paths.root)


if __name__ == '__main__':
    unittest.main()
