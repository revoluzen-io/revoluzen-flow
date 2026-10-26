/**
 * Renderers for a StepResult's rows and schema.
 *
 * Extracted from PreviewPanel.tsx on 2026-10-24, when the node config
 * became a three-pane Input | Parameters | Output view (NodeDataPane.tsx)
 * and needed to draw the same tables the bottom strip draws. Two copies of
 * a data grid drift — one gets the type chips, the other doesn't — and the
 * whole point of showing input beside output is that they are directly
 * comparable, which they are not if they are rendered by different code.
 *
 * Nothing here is new; it is PreviewPanel's own markup, moved and exported.
 */

import type { StepResult } from '../../stores/workflowStore';

// Numeric column types render right-aligned + monospace tabular so the
// digits line up; text / date columns use the normal proportional font,
// which reads better than monospace for natural-language values. Mirrors
// StoragePreviewDrawer.isNumericType.
export function isNumericType(type: string): boolean {
  // No word boundaries on purpose: DuckDB reports "INTEGER", "BIGINT",
  // "HUGEINT", etc. — `\bINT\b` would MISS "INTEGER". Substring match,
  // same as TypeChip's numeric branch, so colour + alignment agree.
  return /INT|DOUBLE|FLOAT|DECIMAL|NUMERIC|REAL|SERIAL/.test(
    (type || '').toUpperCase(),
  );
}

// Small colour-coded chip for a DuckDB/Parquet column type. Same mapping
// as the Storage preview panel (StoragePreviewDrawer.TypeChip).
export function TypeChip({ type }: { type: string }) {
  const t = (type || '').toUpperCase();
  let tone = 'bg-slate-100 text-slate-600 border-slate-200';
  if (/INT|BIGINT|DOUBLE|FLOAT|DECIMAL|NUMERIC|SMALLINT|TINYINT/.test(t)) {
    tone = 'bg-blue-50 text-blue-700 border-blue-200';
  } else if (/VARCHAR|STRING|TEXT|CHAR/.test(t)) {
    tone = 'bg-violet-50 text-violet-700 border-violet-200';
  } else if (/DATE|TIME|TIMESTAMP/.test(t)) {
    tone = 'bg-emerald-50 text-emerald-700 border-emerald-200';
  } else if (/BOOL/.test(t)) {
    tone = 'bg-amber-50 text-amber-800 border-amber-200';
  } else if (/JSON|STRUCT|MAP|LIST|ARRAY/.test(t)) {
    tone = 'bg-rose-50 text-rose-700 border-rose-200';
  }
  return (
    <span
      className={`inline-flex items-center px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider rounded border ${tone}`}
    >
      {t || 'UNKNOWN'}
    </span>
  );
}

/**
 * The sample rows.
 *
 * `highlightColumns` tints a column's header — used by the Output pane to
 * mark columns this step ADDED relative to its input, so "what did my
 * transform do" is answerable by looking rather than by diffing two lists
 * in your head.
 */
export function DataTable({
  result,
  highlightColumns,
}: {
  result: StepResult;
  highlightColumns?: Set<string>;
}) {
  if (!result.sample_data.length) {
    return <div className="p-4 text-slate-500 text-xs">No data</div>;
  }

  // Build a lookup from column name → schema type so the thead can render
  // a coloured TypeChip below each column name (same treatment as the
  // Storage page's bottom preview panel for visual consistency).
  const typeByCol = new Map<string, string>();
  for (const s of result.schema_info) {
    typeByCol.set(s.name, s.type);
  }
  const lastColIdx = result.columns.length - 1;

  return (
    <div className="overflow-auto h-full">
      <table className="w-full text-xs border-collapse">
        {/* Light-but-structured thead. Soft top-down gradient, stronger
            border-b-2, vertical column rules. Type chip below the
            column name uses the schema_info colour map. */}
        <thead className="sticky top-0 z-10 bg-gradient-to-b from-slate-100 to-slate-50 border-b-2 border-slate-200 shadow-[0_2px_4px_-2px_rgba(15,23,42,0.08)]">
          <tr>
            {result.columns.map((col, idx) => {
              const isNew = highlightColumns?.has(col);
              return (
                <th
                  key={col}
                  title={isNew ? `${col} — added by this step` : undefined}
                  className={`px-3 py-2 whitespace-nowrap align-bottom ${
                    isNumericType(typeByCol.get(col) || '') ? 'text-right w-[1%]' : 'text-left'
                  } ${idx < lastColIdx ? 'border-r border-slate-300' : ''} ${
                    isNew ? 'bg-emerald-50' : ''
                  }`}
                >
                  <div
                    className={`text-[11px] font-bold uppercase tracking-wide ${
                      isNew ? 'text-emerald-800' : 'text-slate-800'
                    }`}
                  >
                    {isNew && <span aria-hidden className="mr-0.5">+</span>}
                    {col}
                  </div>
                  {typeByCol.has(col) && (
                    <div className="mt-1">
                      <TypeChip type={typeByCol.get(col)!} />
                    </div>
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody className="bg-white">
          {result.sample_data.map((row, i) => (
            <tr
              key={i}
              className={`border-b border-slate-200 last:border-b-0 hover:bg-amber-50 transition-colors ${
                i % 2 === 1 ? 'bg-slate-50' : 'bg-white'
              }`}
            >
              {result.columns.map((col, idx) => (
                <td
                  key={col}
                  className={`px-3 py-1.5 whitespace-nowrap max-w-48 truncate text-[12px] ${
                    isNumericType(typeByCol.get(col) || '')
                      ? 'text-right tabular-nums font-mono text-slate-700 w-[1%]'
                      : 'text-left text-slate-800'
                  } ${idx < lastColIdx ? 'border-r border-slate-200' : ''}`}
                  title={row[col] === null || row[col] === undefined ? '' : String(row[col])}
                >
                  {row[col] === null ? (
                    <span className="text-slate-500 italic">null</span>
                  ) : (
                    String(row[col])
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function SchemaView({ result }: { result: StepResult }) {
  return (
    <div className="p-3">
      <table className="w-full text-xs border-collapse">
        <thead className="bg-gradient-to-b from-slate-100 to-slate-50 border-b-2 border-slate-200">
          <tr>
            <th className="px-3 py-2 text-left text-[11px] font-bold uppercase tracking-wide text-slate-800 border-r border-slate-300 w-12">#</th>
            <th className="px-3 py-2 text-left text-[11px] font-bold uppercase tracking-wide text-slate-800 border-r border-slate-300">Column</th>
            <th className="px-3 py-2 text-left text-[11px] font-bold uppercase tracking-wide text-slate-800 border-r border-slate-300">Type</th>
            <th className="px-3 py-2 text-left text-[11px] font-bold uppercase tracking-wide text-slate-800">Nullable</th>
          </tr>
        </thead>
        <tbody>
          {result.schema_info.map((col, i) => (
            <tr
              key={i}
              className={`border-b border-slate-200 last:border-b-0 hover:bg-amber-50/50 transition-colors ${
                i % 2 === 1 ? 'bg-slate-50/50' : 'bg-white'
              }`}
            >
              <td className="px-3 py-1.5 text-slate-500 tabular-nums border-r border-slate-200">{i + 1}</td>
              <td className="px-3 py-1.5 text-slate-900 font-mono font-semibold border-r border-slate-200">{col.name}</td>
              <td className="px-3 py-1.5 border-r border-slate-200">
                <TypeChip type={col.type} />
              </td>
              <td className="px-3 py-1.5 text-slate-500">{col.nullable ? 'Yes' : 'No'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function JsonView({ result }: { result: StepResult }) {
  return (
    <pre className="p-3 text-xs text-slate-600 font-mono overflow-auto h-full">
      {JSON.stringify(result.sample_data.slice(0, 10), null, 2)}
    </pre>
  );
}
