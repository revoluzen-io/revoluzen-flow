import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import ApiExplorerPage from '../components/pages/ApiExplorerPage';
import { api } from '../api/client';
import { stageApiConnection, takeApiConnection } from '../utils/apiExplorerDraft';
import { requestFields } from '../components/ApiRequestFields';

vi.mock('../api/client', () => ({ api: { post: vi.fn() } }));
const response = { status: 200, elapsed_ms: 42, bytes: 120, data: { records: [{ id: 1, name: 'Acme' }] }, text: '', headers: {}, truncated: false, sample_limit: 100 };
beforeEach(() => { vi.mocked(api.post).mockReset(); takeApiConnection(); });
afterEach(cleanup);
function show() { render(<ApiExplorerPage />); fireEvent.change(screen.getByLabelText('Request URL'), { target: { value: 'https://example.com/records' } }); }

it('sends and inspects nested output without creating a connector', async () => {
  vi.mocked(api.post).mockResolvedValue(response);
  show(); fireEvent.click(screen.getByRole('button', { name: 'Send request' }));
  expect(await screen.findByText(/HTTP 200/)).toBeVisible();
  fireEvent.click(screen.getByRole('tab', { name: 'Table' }));
  fireEvent.change(screen.getByLabelText('Record path'), { target: { value: '$["records"]' } });
  // Cells render unquoted; the type row under the header carries what the quotes used to.
  expect(screen.getByText('Acme')).toBeVisible();
  expect(within(screen.getByRole('table')).getByText('string')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'Create connection' }));
  expect(takeApiConnection()?.config.base_url).toBe('https://example.com/records');
  expect(api.post).toHaveBeenCalledTimes(1);
});

it('requires confirmation for a write request', async () => {
  vi.mocked(api.post).mockResolvedValue(response);
  show(); fireEvent.change(screen.getByLabelText('HTTP method'), { target: { value: 'DELETE' } });
  fireEvent.click(screen.getByRole('button', { name: 'Send request' }));
  expect(api.post).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Confirm and send' }));
  await screen.findByText(/HTTP 200/);
  expect(api.post).toHaveBeenCalledWith('/connectors/author/explorer/request', expect.objectContaining({ method: 'DELETE', confirm_write: true }));
});

it('clears results when request configuration changes', async () => {
  vi.mocked(api.post).mockResolvedValue(response);
  show(); fireEvent.click(screen.getByRole('button', { name: 'Send request' }));
  await screen.findByText(/HTTP 200/);
  fireEvent.change(screen.getByLabelText('Request URL'), { target: { value: 'https://example.com/other' } });
  expect(screen.queryByRole('button', { name: 'Create connection' })).toBeNull();
});

it('imports methods and path hierarchy without connector generation', async () => {
  vi.mocked(api.post).mockResolvedValue({ base_url: 'https://example.com', operations: [{ path: '/accounts/{id}/orders', method: 'GET', summary: '', security: [] }] });
  show();
  fireEvent.change(screen.getByLabelText('API specification'), { target: { value: '{"paths":{}}' } });
  fireEvent.click(screen.getByRole('button', { name: 'Discover endpoints' }));
  const select = await screen.findByLabelText('Discovered endpoint');
  fireEvent.change(select, { target: { value: '0' } });
  expect(screen.getByLabelText('Request URL')).toHaveValue('https://example.com/accounts/{id}/orders');
});

it('keeps errors visible and does not invent a response', async () => {
  vi.mocked(api.post).mockRejectedValue(new Error('Network blocked'));
  show(); fireEvent.click(screen.getByRole('button', { name: 'Send request' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Network blocked');
  expect(screen.queryByRole('button', { name: 'Create connection' })).toBeNull();
});

it('never transfers query secrets or persists a handoff', () => {
  stageApiConnection('https://example.com/path?api_key=CANARY', 'api_key_query', 'api_key');
  const draft = takeApiConnection();
  expect(JSON.stringify(draft)).not.toContain('CANARY');
  expect(takeApiConnection()).toBeNull();
});

it('sends enabled key-value rows and removes disabled ones', async () => {
  vi.mocked(api.post).mockResolvedValue(response);
  show();
  // Request settings are one scrolling form now — no tab click needed.
  fireEvent.change(screen.getByLabelText('parameter name 1'), { target: { value: 'limit' } });
  fireEvent.change(screen.getByLabelText('parameter value 1'), { target: { value: '20' } });
  fireEvent.click(screen.getByRole('button', { name: 'Add parameter' }));
  fireEvent.change(screen.getByLabelText('parameter name 2'), { target: { value: 'ignored' } });
  fireEvent.click(screen.getByLabelText('Enable parameter 2'));
  fireEvent.click(screen.getByRole('button', { name: 'Send request' }));
  await screen.findByText(/HTTP 200/);
  expect(api.post).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ query: { limit: '20' } }));
  fireEvent.click(screen.getByRole('button', { name: 'Remove parameter 1' }));
  expect(screen.queryByDisplayValue('limit')).toBeNull();
});

it('shows the whole request at once, with credentials kept in the form', () => {
  show();
  fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: 'bearer' } });
  fireEvent.change(screen.getByLabelText('Bearer token'), { target: { value: 'ephemeral' } });
  // Auth, params, headers and body are all present without navigating.
  expect(screen.getByLabelText('parameter name 1')).toBeVisible();
  expect(screen.getByLabelText('header name 1')).toBeVisible();
  expect(screen.getByLabelText('Bearer token')).toHaveValue('ephemeral');
  // The request pane no longer has a tab strip; only the response pane does.
  expect(screen.queryByRole('tablist', { name: 'Request settings' })).toBeNull();
});

it('guides arrival: later steps are unreachable until earlier ones are done', () => {
  render(<ApiExplorerPage />);
  expect(screen.getByRole('button', { name: /Choose an endpoint/ })).toBeEnabled();
  expect(screen.getByRole('button', { name: /Test it/ })).toBeDisabled();
  expect(screen.getByRole('button', { name: /Generate a connector/ })).toBeDisabled();
  // A greyed Send button must not be the only explanation of what to do.
  expect(screen.getByText(/Enter the URL you want to test/i)).toBeVisible();
});

it('navigates to the connector step from the rail once a response exists', async () => {
  vi.mocked(api.post).mockResolvedValue(response);
  show();
  fireEvent.click(screen.getByRole('button', { name: 'Send request' }));
  await screen.findByText(/HTTP 200/);

  const step3 = screen.getByRole('button', { name: /Generate a connector/ });
  expect(step3).toBeEnabled();
  fireEvent.click(step3);
  expect(screen.getByRole('tab', { name: 'Connector' })).toHaveAttribute('aria-selected', 'true');
  expect(screen.getByLabelText('Connector id')).toBeVisible();
});

it('rejects duplicate or unnamed enabled fields', () => {
  const row = { id: '1', enabled: true, key: 'a', value: 'b' };
  expect(() => requestFields([row, { ...row, id: '2' }])).toThrow('Duplicate');
  expect(() => requestFields([{ ...row, key: '' }])).toThrow('Enter a name');
});

const xmlResponse = {
  status: 200, elapsed_ms: 12, bytes: 200, truncated: false, headers: {}, sample_limit: 100,
  kind: 'xml', content_type: 'application/xml',
  data: { orders: { order: [{ '@id': '1', sku: 'A' }, { '@id': '2', sku: 'B' }] } },
  text: '<orders>\n  <order id="1">\n    <sku>A</sku>\n  </order>\n</orders>',
  xml: {
    records: [
      { path: '/orders/order', count: 2, rows: [{ id: '1', sku: 'A' }, { id: '2', sku: 'B' }] },
      { path: '/orders/order/sku', count: 2, rows: [{ value: 'A' }, { value: 'B' }] },
    ],
    structure: [{ path: '/orders/order', type: 'element', count: 2 }, { path: '/orders/order/@id', type: 'attribute', count: 2 }],
    fault: null,
  },
};
const sendRequest = async () => { fireEvent.click(screen.getByRole('button', { name: 'Send request' })); return screen.findByText(/HTTP 200/); };

it('shows an XML reply formatted, as rows from its repeating element and in Structure', async () => {
  vi.mocked(api.post).mockResolvedValue(xmlResponse);
  show(); await sendRequest();
  expect(screen.getByRole('tab', { name: 'XML' })).toBeVisible();
  expect(screen.getByLabelText('Formatted XML')).toHaveTextContent('<sku>A</sku>');
  // A REST connection reads XML too, through the Source step's Row Path.
  expect(screen.getByRole('button', { name: 'Create connection' })).toBeVisible();
  fireEvent.click(screen.getByRole('tab', { name: 'Table' }));
  expect(screen.getByLabelText('Row path')).toHaveValue('/orders/order');
  expect(screen.getByRole('table')).toHaveTextContent('B');
  fireEvent.change(screen.getByLabelText('Row path'), { target: { value: '/orders/order/sku' } });
  expect(within(screen.getByRole('table')).getByText('value')).toBeVisible();
  fireEvent.click(screen.getByRole('tab', { name: 'Structure' }));
  expect(screen.getByText('/orders/order/@id')).toBeVisible();
  expect(screen.getByText('attribute × 2')).toBeVisible();
});

it('says when a SOAP service answered with a fault, and will not make a connector from it', async () => {
  vi.mocked(api.post).mockResolvedValue({ ...xmlResponse, status: 500, xml: { ...xmlResponse.xml, fault: { code: 's:Client', message: 'Unknown region', detail: '' } } });
  show(); fireEvent.click(screen.getByRole('button', { name: 'Send request' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Unknown region');
  fireEvent.click(screen.getByRole('tab', { name: 'Connector' }));
  expect(screen.getByRole('button', { name: /Generate connector draft/ })).toBeDisabled();
});

it('shows other kinds of reply for what they are', async () => {
  const kinds = [
    { kind: 'binary', binary: 'PNG image', data: null, text: '', expected: /PNG image/ },
    { kind: 'html', title: 'Sign in', data: null, text: '<html></html>', expected: /web page: Sign in/ },
  ];
  for (const { expected, ...reply } of kinds) {
    vi.mocked(api.post).mockResolvedValue({ ...response, ...reply });
    show(); await sendRequest();
    expect(screen.getByText(expected)).toBeVisible();
    cleanup();
  }
});

it('shows a CSV reply as its text and as a table', async () => {
  vi.mocked(api.post).mockResolvedValue({ ...response, kind: 'csv', data: [{ id: '1', name: 'Acme' }], text: 'id,name\n1,Acme' });
  show(); await sendRequest();
  expect(screen.getByRole('tab', { name: 'CSV' })).toBeVisible();
  expect(screen.getByText(/id,name/)).toBeVisible();
  fireEvent.click(screen.getByRole('tab', { name: 'Table' }));
  expect(screen.getByRole('table')).toHaveTextContent('Acme');
});

function postRequest(bodyType: string) {
  show();
  fireEvent.change(screen.getByLabelText('HTTP method'), { target: { value: 'POST' } });
  fireEvent.click(screen.getByRole('radio', { name: bodyType }));
}

it('sends the body type that was chosen', async () => {
  vi.mocked(api.post).mockResolvedValue(response);
  postRequest('XML');
  fireEvent.change(screen.getByLabelText('Request body'), { target: { value: '<a>1</a>' } });
  fireEvent.click(screen.getByRole('button', { name: 'Send request' }));
  fireEvent.click(screen.getByRole('button', { name: 'Confirm and send' }));
  await screen.findByText(/HTTP 200/);
  expect(api.post).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ body: '<a>1</a>', body_type: 'xml' }));
});

it('warns about XML that is not well-formed, and sends it anyway if asked', () => {
  postRequest('XML');
  fireEvent.change(screen.getByLabelText('Request body'), { target: { value: '<a><b></a>' } });
  expect(screen.getByText(/not well-formed XML/)).toBeVisible();
  expect(screen.getByRole('button', { name: 'Send request' })).toBeEnabled();
});

it('encodes form fields', async () => {
  vi.mocked(api.post).mockResolvedValue(response);
  postRequest('Form');
  fireEvent.change(screen.getByLabelText('form field name 1'), { target: { value: 'q' } });
  fireEvent.change(screen.getByLabelText('form field value 1'), { target: { value: 'a b&c' } });
  fireEvent.click(screen.getByRole('button', { name: 'Send request' }));
  fireEvent.click(screen.getByRole('button', { name: 'Confirm and send' }));
  await screen.findByText(/HTTP 200/);
  expect(api.post).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ body: 'q=a+b%26c', body_type: 'form' }));
});

const envelope = '<e><!--Optional:--><status>{status}</status><region>{region}</region></e>';
const soapImport = {
  kind: 'soap', base_url: '', service: 'Customers', warnings: ['Check this one thing.'],
  operations: [{
    method: 'POST', path: 'GetCustomers', summary: 'List customers', security: [],
    soap: {
      operation: 'GetCustomers', service: 'Customers', port: 'P', version: '1.1', endpoint: 'https://example.com/soap',
      headers: { 'Content-Type': 'text/xml; charset=utf-8', SOAPAction: '"urn:x"' }, envelope, warnings: [],
      fields: [
        { name: 'region', label: 'region', type: 'string', required: true, path: 'GetCustomers/region' },
        { name: 'status', label: 'status', type: 'string', required: false, path: 'GetCustomers/status' },
      ],
    },
  }],
};

async function importWsdl() {
  vi.mocked(api.post).mockResolvedValueOnce(soapImport);
  render(<ApiExplorerPage />);
  fireEvent.change(screen.getByLabelText('API specification'), { target: { value: '<definitions/>' } });
  fireEvent.click(screen.getByRole('button', { name: 'Discover endpoints' }));
  fireEvent.change(await screen.findByLabelText('Discovered endpoint'), { target: { value: '0' } });
}

it('fills in the request from a WSDL operation and asks for its required values', async () => {
  await importWsdl();
  expect(api.post).toHaveBeenCalledWith('/connectors/author/explorer/wsdl', { text: '<definitions/>' });
  expect(screen.getByText('Check this one thing.')).toBeVisible();
  expect(screen.getByLabelText('Request URL')).toHaveValue('https://example.com/soap');
  expect(screen.getByLabelText('HTTP method')).toHaveValue('POST');
  expect(screen.getByLabelText('Request body')).toHaveValue(envelope);
  expect(screen.getByRole('radio', { name: 'XML' })).toBeChecked();
  expect(screen.getByLabelText('header name 2')).toHaveValue('SOAPAction');

  fireEvent.click(screen.getByRole('button', { name: 'Send request' }));
  expect(screen.getByRole('alert')).toHaveTextContent('Fill in: region');
  expect(api.post).toHaveBeenCalledTimes(1);

  vi.mocked(api.post).mockResolvedValue(xmlResponse);
  fireEvent.change(screen.getByLabelText('Value for region'), { target: { value: 'EMEA' } });
  fireEvent.click(screen.getByRole('button', { name: 'Send request' }));
  fireEvent.click(screen.getByRole('button', { name: 'Confirm and send' }));
  await screen.findByText(/HTTP 200/);
  expect(api.post).toHaveBeenLastCalledWith('/connectors/author/explorer/request', expect.objectContaining({
    body: envelope, body_type: 'xml', body_values: { status: '', region: 'EMEA' },
    headers: { 'Content-Type': 'text/xml; charset=utf-8', SOAPAction: '"urn:x"' },
  }));
});

it('makes a tried SOAP request into a connector, with the row path and values', async () => {
  await importWsdl();
  vi.mocked(api.post).mockResolvedValue(xmlResponse);
  fireEvent.change(screen.getByLabelText('Value for region'), { target: { value: 'EMEA' } });
  fireEvent.click(screen.getByRole('button', { name: 'Send request' }));
  fireEvent.click(screen.getByRole('button', { name: 'Confirm and send' }));
  await screen.findByText(/HTTP 200/);

  fireEvent.click(screen.getByRole('tab', { name: 'Connector' }));
  expect(screen.getByRole('radio', { name: /This request/ })).toBeChecked();
  const manifest = { id: 'example', name: 'Example', streams: [{ name: 'getcustomers', path: '/soap' }] };
  vi.mocked(api.post).mockResolvedValue({ manifest, validation: { valid: true, errors: [], warnings: [] }, mode: 'request', runtime_manifest: manifest });
  fireEvent.click(screen.getByRole('button', { name: /Generate connector draft/ }));
  expect(await screen.findByRole('button', { name: 'Save as Beta connector' })).toBeVisible();
  expect(api.post).toHaveBeenLastCalledWith('/connectors/author/from-request', expect.objectContaining({
    connector_id: 'example', url: 'https://example.com/soap', method: 'POST', operation: 'GetCustomers',
    row_path: '/orders/order', body: envelope, body_type: 'xml',
    fields: [
      { name: 'status', label: 'status', required: false, default: '' },
      { name: 'region', label: 'region', required: true, default: 'EMEA' },
    ],
  }));
  // The security review lists OpenAPI endpoints, which a single request does not have.
  expect(screen.queryByRole('region', { name: 'Operation authentication' })).toBeNull();
});

it('loads a WSDL by its address, sending the sign-in with that download', async () => {
  vi.mocked(api.post).mockResolvedValue(soapImport);
  render(<ApiExplorerPage />);
  fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: 'bearer' } });
  fireEvent.change(screen.getByLabelText('Bearer token'), { target: { value: 'ephemeral' } });
  fireEvent.change(screen.getByLabelText('WSDL address'), { target: { value: 'https://example.com/s?wsdl' } });
  fireEvent.click(screen.getByRole('button', { name: 'Load WSDL' }));
  expect(await screen.findByText('Imported: 1 operation from Customers')).toBeVisible();
  expect(api.post).toHaveBeenCalledWith('/connectors/author/explorer/wsdl', expect.objectContaining({ url: 'https://example.com/s?wsdl', auth_type: 'bearer', token: 'ephemeral' }));
});
