/**
 * The built-in templates must be made of steps the editor offers, joined the way
 * the editor joins them, and load into the editor whole.
 *
 * What a step's settings must be (the names its node reads) is held by
 * backend/tests/test_template_steps_match_nodes.py against each node's
 * `param_schema()`, because only the backend can see the nodes. This file holds
 * what the frontend can: the palette, the connector names, the wiring, the
 * parameters a webhook template declares, and the load itself.
 *
 * Why it exists: `switch_case` and `webhook_trigger` were still used by two
 * templates after the palette dropped them ("Switch" is `conditional_split`; an
 * inbound webhook cannot hand a pipeline rows), `http_trigger`, `oracle_fusion`
 * and a `slack` destination were connectors no node has, and connections carried
 * a `condition` that nothing reads.
 */
import { describe, expect, it } from 'vitest';

import { VALID_GHOST_TYPES } from '../components/modulesPanelData';
import { TEMPLATE_CATALOG, getTemplate } from '../templates/catalog';
import { useWorkflowStore } from '../stores/workflowStore';
import { branchPortsFor } from '../utils/branchPorts';
import { validateWorkflow } from '../utils/validateWorkflow';

// The connectors the generic Source and Destination route to: SOURCE_MAP and
// DEST_MAP in backend/revoluzen_flow/nodes/generic.py. The frontend cannot see the
// backend, so the names are written out here, as in nodeDocsCoverage.test.ts;
// the backend test holds each template step to the real schema.
const SOURCE_CONNECTORS = [
  'csv', 'json', 'parquet', 'excel', 'xml', 'database', 'rest_api', 's3', 'azure_blob', 'gcs',
  'sharepoint', 'onedrive', 'kafka', 'ftp', 'sftp', 'gsheet', 'delta', 'microsoft_graph', 'local_table',
];
const DESTINATION_CONNECTORS = [
  'csv', 'json', 'parquet', 'excel', 'database', 's3', 'azure_blob', 'gcs', 'sharepoint', 'onedrive',
  'kafka', 'ftp', 'sftp', 'rest_api', 'webhook', 'email', 'delta', 'warehouse', 'local_table',
];
const FILE_CONNECTORS = ['csv', 'json', 'parquet', 'excel', 'xml'];

const CONNECTION_KEYS = ['from_step', 'to_step', 'from_port'];
const PARAMETER_TYPES = ['string', 'int', 'float', 'bool', 'json'];

const steps = TEMPLATE_CATALOG.flatMap((template) => template.steps.map((step) => ({ template, step })));
const where = ({ template, step }: (typeof steps)[number]): string => `${template.id} / ${step.id} "${step.label}"`;

describe('built-in templates: step types', () => {
  it('has templates to check', () => {
    expect(TEMPLATE_CATALOG.length).toBeGreaterThan(0);
    expect(new Set(TEMPLATE_CATALOG.map((t) => t.id)).size).toBe(TEMPLATE_CATALOG.length);
  });

  it('use only types the palette offers', () => {
    const missing = steps.filter(({ step }) => !VALID_GHOST_TYPES.has(step.type));
    expect(missing.map((s) => `${where(s)} is "${s.step.type}"`)).toEqual([]);
  });

  it('give every step an id of its own and a label', () => {
    const problems: string[] = [];
    for (const template of TEMPLATE_CATALOG) {
      const ids = template.steps.map((s) => s.id);
      if (new Set(ids).size !== ids.length) problems.push(`${template.id}: step ids repeat`);
      for (const s of template.steps) if (!s.label.trim()) problems.push(`${template.id} / ${s.id} has no label`);
    }
    expect(problems).toEqual([]);
  });

  it('name a connector that a Source or Destination has', () => {
    const problems = steps
      .filter(({ step }) => step.type === 'source' || step.type === 'destination')
      .flatMap((s) => {
        const connector = String(s.step.params.connector_type ?? '');
        const known = s.step.type === 'source' ? SOURCE_CONNECTORS : DESTINATION_CONNECTORS;
        return known.includes(connector) ? [] : [`${where(s)}: "${connector}" is not a ${s.step.type} connector`];
      });
    expect(problems).toEqual([]);
  });

  it('leave the connection and the URL to the user for a network connector', () => {
    const problems = steps
      .filter(({ step }) => step.type === 'source' || step.type === 'destination')
      .filter(({ step }) => !FILE_CONNECTORS.includes(String(step.params.connector_type)))
      .flatMap((s) =>
        ['connection_id', 'url', 'password', 'token', 'api_key']
          .filter((key) => key in s.step.params)
          .map((key) => `${where(s)} sets ${key}`),
      );
    expect(problems).toEqual([]);
  });
});

describe('built-in templates: connections', () => {
  it('carry only the keys a saved connection has, and join steps that exist', () => {
    const problems: string[] = [];
    for (const template of TEMPLATE_CATALOG) {
      const ids = new Set(template.steps.map((s) => s.id));
      for (const c of template.connections) {
        const stray = Object.keys(c).filter((k) => !CONNECTION_KEYS.includes(k));
        if (stray.length) problems.push(`${template.id}: ${c.from_step} -> ${c.to_step} sets ${stray.join(', ')}`);
        if (!ids.has(c.from_step) || !ids.has(c.to_step)) {
          problems.push(`${template.id}: ${c.from_step} -> ${c.to_step} joins a step that is not there`);
        }
      }
    }
    expect(problems).toEqual([]);
  });

  it('leave a branching step by a named output, and read every case it makes', () => {
    const problems: string[] = [];
    for (const template of TEMPLATE_CATALOG) {
      for (const step of template.steps) {
        const ports = branchPortsFor(step.type, step.params);
        if (ports.length < 2) continue;
        const out = template.connections.filter((c) => c.from_step === step.id);
        for (const c of out) {
          if (!c.from_port) problems.push(`${template.id}: ${c.from_step} -> ${c.to_step} leaves a ${step.type} by its default output`);
        }
        // A Switch case that nothing reads is a branch that ends nowhere. The default
        // output is allowed to lead nowhere: it is where unmatched rows are let go.
        if (step.type === 'conditional_split') {
          const used = new Set(out.map((c) => c.from_port));
          const fallback = String(step.params.default_output ?? 'default');
          for (const p of ports) {
            if (p.id !== fallback && !used.has(p.id)) problems.push(`${template.id}: no step reads the "${p.id}" case of ${step.id}`);
          }
        }
      }
    }
    expect(problems).toEqual([]);
  });
});

describe('built-in templates: parameters', () => {
  const refs = /\$\{param\.([A-Za-z_][A-Za-z0-9_]*)\}/g;
  const strings = (value: unknown): string[] =>
    typeof value === 'string'
      ? [value]
      : Array.isArray(value)
        ? value.flatMap(strings)
        : value && typeof value === 'object'
          ? Object.values(value).flatMap(strings)
          : [];

  it('declare every ${param.NAME} a step refers to', () => {
    const problems: string[] = [];
    for (const template of TEMPLATE_CATALOG) {
      const declared = new Set((template.parameters ?? []).map((p) => p.name));
      for (const step of template.steps) {
        for (const text of strings(step.params)) {
          for (const m of text.matchAll(refs)) {
            if (!declared.has(m[1])) problems.push(`${template.id} / ${step.id} uses \${param.${m[1]}} with no parameter`);
          }
        }
      }
    }
    expect(problems).toEqual([]);
  });

  it('are complete: a valid name, a type the editor has, a description', () => {
    const problems: string[] = [];
    for (const template of TEMPLATE_CATALOG) {
      for (const p of template.parameters ?? []) {
        if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(p.name)) problems.push(`${template.id}: "${p.name}" is not a usable name`);
        if (!PARAMETER_TYPES.includes(p.type)) problems.push(`${template.id}: ${p.name} has type "${p.type}"`);
        if (!p.description.trim()) problems.push(`${template.id}: ${p.name} has no description`);
      }
    }
    expect(problems).toEqual([]);
  });

  it('are declared by every template a webhook starts', () => {
    // A request body becomes the declared parameters; an undeclared key is refused.
    const webhook = TEMPLATE_CATALOG.filter((t) => t.tags.includes('Webhook'));
    expect(webhook.length).toBeGreaterThan(0);
    expect(webhook.filter((t) => !(t.parameters ?? []).length).map((t) => t.id)).toEqual([]);
  });
});

describe("the editor's own starter pipelines", () => {
  // workflowStore keeps a copy of the planner's starters (dedup_pipeline, aggregation,
  // data_quality) that the chat panel loads. The Aggregation one asked for
  // `aggregations`; the Aggregate node reads `functions`.
  it.each(['simple_etl', 'dedup_pipeline', 'aggregation', 'data_quality'])('%s loads with settings its steps read', async (key) => {
    await useWorkflowStore.getState().useTemplate(key);
    const { nodes } = useWorkflowStore.getState();
    expect(nodes.length).toBeGreaterThan(0);
    for (const n of nodes) {
      const { stepType, params } = n.data as { stepType: string; params: Record<string, unknown> };
      if (stepType === 'aggregate') {
        expect(Object.keys(params)).not.toContain('aggregations');
        expect(Array.isArray(params.functions) && (params.functions as unknown[]).length > 0).toBe(true);
      }
    }
  });
});

describe('loading a built-in template into the editor', () => {
  it.each(TEMPLATE_CATALOG.map((t) => [t.id]))('%s arrives whole', async (id) => {
    const template = getTemplate(id)!;
    await useWorkflowStore.getState().useTemplate(id);
    const state = useWorkflowStore.getState();

    expect(state.nodes.map((n) => n.id).sort()).toEqual(template.steps.map((s) => s.id).sort());
    expect(state.edges).toHaveLength(template.connections.length);
    expect(state.parameters.map((p) => p.name)).toEqual((template.parameters ?? []).map((p) => p.name));

    // A named output is the edge's source handle, so the canvas draws it from that port.
    for (const c of template.connections.filter((x) => x.from_port)) {
      const edge = state.edges.find((e) => e.source === c.from_step && e.target === c.to_step && e.sourceHandle === c.from_port);
      expect(edge, `${c.from_step} -> ${c.to_step} by ${c.from_port}`).toBeTruthy();
    }
  });

  it.each(TEMPLATE_CATALOG.map((t) => [t.id]))(
    '%s can be run from the editor once the user has picked its connections and the Slack webhook',
    async (id) => {
      await useWorkflowStore.getState().useTemplate(id);
      const { nodes, edges, parameters } = useWorkflowStore.getState();
      // What the user does after loading: pick a saved connection on every step that
      // reaches another system, and give Slack its webhook. Nothing else is theirs to add.
      const picked = nodes.map((n) => {
        const data = n.data as { stepType: string; params: Record<string, unknown> };
        const network =
          (data.stepType === 'source' || data.stepType === 'destination') &&
          !FILE_CONNECTORS.includes(String(data.params.connector_type));
        const extra = network
          ? { connection_id: 'a-saved-connection' }
          : data.stepType === 'slack_notify'
            ? { webhook_url: 'https://hooks.example.test/services/x' }
            : null;
        return extra ? { ...n, data: { ...data, params: { ...data.params, ...extra } } } : n;
      });
      const errors = validateWorkflow(picked, edges, parameters).filter((i) => i.level === 'error');
      expect(errors.map((i) => `${i.nodeLabel}: ${i.message}`)).toEqual([]);
    },
  );

  it.each(TEMPLATE_CATALOG.map((t) => [t.id]))(
    '%s asks the user for nothing but a connection or a Slack webhook before it runs',
    async (id) => {
      await useWorkflowStore.getState().useTemplate(id);
      const { nodes, edges, parameters } = useWorkflowStore.getState();
      const issues = validateWorkflow(nodes, edges, parameters).filter((i) => i.level === 'error');
      const unexpected = issues.filter((i) => !/connection|webhook_url|webhook url/i.test(i.message));
      expect(unexpected.map((i) => `${i.nodeLabel}: ${i.message}`)).toEqual([]);
    },
  );
});
