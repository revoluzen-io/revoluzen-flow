import { type ReactNode, useMemo } from 'react';

/**
 * Formatted XML for the API Explorer response pane.
 *
 * The server sends this already indented and with sensitive values masked. Here
 * it is only coloured: every piece stays text in a text node, so nothing in a
 * reply is ever treated as markup.
 */

const TOKEN = /<!--[\s\S]*?-->|<\?[\s\S]*?\?>|<(\/?)([^\s/>]+)([^<>]*?)(\/?)>|[^<]+|</g;
const ATTRIBUTE = /([^\s=]+)(\s*=\s*)("[^"]*"|'[^']*')/g;
// Colouring a very large reply one span at a time would stall the page.
const COLOUR_LIMIT = 120_000;

function attributes(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(ATTRIBUTE)) {
    out.push(text.slice(last, match.index));
    out.push(<span key={`${key}n${match.index}`} className="xv-attr">{match[1]}</span>, match[2]);
    out.push(<span key={`${key}v${match.index}`} className="xv-value">{match[3]}</span>);
    last = (match.index as number) + match[0].length;
  }
  out.push(text.slice(last));
  return out;
}

function colour(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let index = 0;
  for (const match of text.matchAll(TOKEN)) {
    const key = String(index++);
    const piece = match[0];
    if (match[2]) {
      out.push(
        <span key={key} className="xv-punctuation">{`<${match[1]}`}</span>,
        <span key={`${key}t`} className="xv-tag">{match[2]}</span>,
        ...attributes(match[3], key),
        <span key={`${key}e`} className="xv-punctuation">{`${match[4]}>`}</span>,
      );
    } else if (piece.startsWith('<!--') || piece.startsWith('<?')) {
      out.push(<span key={key} className="xv-comment">{piece}</span>);
    } else {
      out.push(piece);
    }
  }
  return out;
}

export default function ApiXmlView({ text }: { text: string }) {
  const content = useMemo(() => (text.length > COLOUR_LIMIT ? text : colour(text)), [text]);
  return <pre className="api-response-pre xv-root" aria-label="Formatted XML">{content}</pre>;
}
