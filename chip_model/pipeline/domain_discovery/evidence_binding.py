"""Conservative local scope checks; passing is never semantic approval."""
import re


def _parts(text, separator):
    start = 0
    for match in re.finditer(separator, text):
        if text[start:match.start()].strip():
            yield start, match.start(), text[start:match.start()]
        start = match.end()
    if text[start:].strip():
        yield start, len(text), text[start:]


def _sku_in(text, sku):
    # Only whitespace/case equivalence; never remove capacity or SKU suffixes.
    literal = r'\s+'.join(re.escape(word) for word in sku.split())
    return re.search(r'(?<![A-Za-z0-9_-])' + literal + r'(?![A-Za-z0-9_-])', text, re.I) is not None


def numeric_sku_binding(sku, vendor, evidence, context_refs, has_value, scope_boundaries=()):
    """Use only the primary quote and declared exact structural references.

    Looking up an SKU elsewhere in the page cannot qualify a value. Sentences
    and clauses stay separate, except an explicit shared-subject quantifier.
    Ambiguous layouts remain unresolved rather than guessed by the program.
    """
    def proof(mode, start, end, quote):
        return {'mode': mode, 'target_sku': sku, 'start': start, 'end': end, 'quote': quote}

    for sent_start, sent_end, sentence in _parts(evidence.quote, r'[。！？!?;；\n]|\.(?=\s|$)'):
        for start, end, clause in _parts(sentence, r'[,，]'):
            if not has_value(clause):
                continue
            if _sku_in(clause, sku):
                return proof('direct_claim', evidence.start+sent_start+start,
                             evidence.start+sent_start+end, clause)
            if (_sku_in(sentence, sku)
                    and re.match(r'\s*(?:均|皆|都|两款芯片|二者|两者|both\b|all\b)', clause, re.I)):
                return proof('explicit_shared_claim', evidence.start+sent_start,
                             evidence.start+sent_end, sentence)

    normalize = lambda value: ' '.join(value.strip(' #:\t\r\n：').split()).casefold()
    exclusive = {normalize(sku), normalize(vendor+' '+sku)}
    headers = [ref for ref in context_refs if ref.kind in {'heading', 'table_header'}
               and ref.end <= evidence.start and normalize(ref.quote) in exclusive]
    # An explicit different model in the value quote cannot inherit a heading.
    # A model-like token without a proven binding is held conservatively.
    named_subject = re.search(r'[A-Za-z][A-Za-z_-]*(?:[ \t]+[A-Za-z_-]+)*[ \t]*\d+[A-Za-z0-9_-]*', evidence.quote)
    if headers and not named_subject:
        header = max(headers, key=lambda ref: ref.end)
        # Boundaries come from the saved page/chunk, not the model's chosen
        # subset: omitting the next SKU heading cannot extend this heading.
        intervening = any(ref['kind'] in {'heading', 'table_header'}
                          and header.end <= ref['start'] < evidence.start
                          for ref in scope_boundaries)
        if not intervening and has_value(evidence.quote):
            return {**proof('exclusive_header', evidence.start, evidence.end, evidence.quote),
                    'header': header.model_dump()}
    return None
