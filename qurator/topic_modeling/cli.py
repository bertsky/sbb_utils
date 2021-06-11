import sqlite3
import pandas as pd
# from scipy.sparse import csr_matrix
import click
# import gensim
from gensim.models.ldamulticore import LdaMulticore
from tqdm import tqdm
from qurator.utils.parallel import run as prun
import json
from ..sbb.ned import count_entities as _count_entities
from ..sbb.ned import parse_sentence
import os
# from gensim.corpora.dictionary import Dictionary
# from pyLDAvis.gensim import prepare


def count_entities(ner):
    counter = {}

    _count_entities(ner, counter, min_len=0)

    df = pd.DataFrame.from_dict(counter, orient='index', columns=['count'])

    return df


def make_bow(data):
    docs = []
    ppns = []
    for ppn, doc in tqdm(data.groupby('ppn')):
        docs.append([(int(voc_index), float(wcount)) for doc_len, voc_index, wcount in
                     zip(doc.doc_len, doc.voc_index.tolist(), doc.wcount.tolist())])
        ppns.append(ppn)

    return docs, ppns


class ParseJob:

    voc = None
    con = None

    def __init__(self, ppn, part):
        self._ppn = ppn
        self._part = part

    def __call__(self, *args, **kwargs):

        df = pd.read_sql('SELECT * from tagged where ppn=?', con=ParseJob.con, params=(self._ppn,))

        df['page'] = df.file_name.str.extract('([1-9][0-9]*)').astype(int)

        df = df.loc[(df.page >= self._part.start_page.min()) & (df.page <= self._part.stop_page.max())]

        def iterate_entities():
            for _, row in df.iterrows():
                ner = \
                    [[{'word': word, 'prediction': tag} for word, tag in zip(sen_text, sen_tags)]
                     for sen_text, sen_tags in zip(json.loads(row.text), json.loads(row.tags))]

                for sent in ner:
                    entity_ids, entities, entity_types = parse_sentence(sent)

                    for entity_id, entity, ent_type in zip(entity_ids, entities, entity_types):

                        if entity_id == "-":
                            continue

                        yield entity_id, entity, ent_type

        doc = pd.DataFrame([(pos, eid) for pos, (eid, entity, ent_type) in enumerate(iterate_entities())],
                           columns=['pos', 'entity_id'])

        doc = doc.merge(self._part, on='entity_id', how='left').reset_index(drop=True)

        doc['ppn'] = self._ppn

        return doc

    @staticmethod
    def initialize(voc, sqlite_file):

        ParseJob.voc = voc
        ParseJob.con = sqlite3.connect(sqlite_file)


def read_docs(sqlite_file, processes, min_surface_len=2, min_proba=0.25, entities_file=None):

    entities = None
    if entities_file is not None:

        print("Reading id2work information from entities table ...")
        with sqlite3.connect(entities_file) as con:
            entities = pd.read_sql('SELECT * from entities', con=con).set_index('QID')

    with sqlite3.connect(sqlite_file) as con:

        print('Reading entity linking table ...')
        df = pd.read_sql('SELECT * from entity_linking', con=con).drop(columns=["index"]).reset_index(drop=True)
        print('done.')

        df = df.loc[(df.proba > min_proba) & (df.page_title.str.len() > min_surface_len)
                    & (df.entity_id.str.len() > min_surface_len + 4)]

        df = df.loc[df.wikidata.str.startswith('Q')]

        voc = {qid: i for i, qid in enumerate(df.wikidata.unique())}

        data = []

        def get_jobs():
            for ppn, part in tqdm(df.groupby('ppn')):
                yield ParseJob(ppn, part)

        for i, tmp in enumerate(prun(get_jobs(), initializer=ParseJob.initialize, initargs=(voc, sqlite_file),
                                     processes=processes)):
            data.append(tmp)

        data = pd.concat(data)

        if entities is not None:
            data = data.merge(entities[['label']], left_on='wikidata', right_index=True, how='left')

    return data, voc


@click.command()
@click.argument('sqlite-file', type=click.Path(exists=True), required=True, nargs=1)
@click.argument('docs-file', type=click.Path(exists=False), required=True, nargs=1)
@click.option('--processes', default=4, help='Number of workers.')
@click.option('--min-proba', type=float, default=0.25, help='Minimum probability of counted entities.')
@click.option('--entities-file', default=None, help="Knowledge-base of entity linking step.")
def extract_docs(sqlite_file, docs_file, processes, min_proba, entities_file):

    data, voc = read_docs(sqlite_file, processes=processes, min_proba=min_proba, entities_file=entities_file)

    data.to_pickle(docs_file)


class CountJob:

    voc = None
    con = None

    def __init__(self, ppn, part):
        self._ppn = ppn
        self._part = part

    def __call__(self, *args, **kwargs):

        df = pd.read_sql('SELECT * from tagged where ppn=?', con=CountJob.con, params=(self._ppn,))

        df['page'] = df.file_name.str.extract('([1-9][0-9]*)').astype(int)

        df = df.loc[(df.page >= self._part.start_page.min()) & (df.page <= self._part.stop_page.max())]

        cnt = []
        doc_len = 0
        for _, row in df.iterrows():
            ner = \
                [[{'word': word, 'prediction': tag} for word, tag in zip(sen_text, sen_tags)]
                 for sen_text, sen_tags in zip(json.loads(row.text), json.loads(row.tags))]

            doc_len += sum([len(s) for s in ner])

            counter = count_entities(ner)

            counter = counter.merge(self._part, left_index=True, right_on='entity_id')

            counter['on_page'] = row.page

            cnt.append(counter)

        cnt = pd.concat(cnt)

        weighted_cnt = []
        for (qid, page_title), qpart in cnt.groupby(['wikidata', 'page_title']):

            weighted_count = qpart[['count']].T.dot(qpart[['proba']]).iloc[0].iloc[0]

            weighted_cnt.append((qid, page_title, weighted_count))

        weighted_cnt = pd.DataFrame(weighted_cnt, columns=['wikidata', 'page_title', 'wcount']).\
            sort_values('wcount', ascending=False).reset_index(drop=True)

        tmp = pd.DataFrame([(qid, CountJob.voc[qid], wcount)
                            for qid, wcount in zip(weighted_cnt.wikidata.tolist(), weighted_cnt.wcount.tolist())],
                           columns=['wikidata', 'voc_index', 'wcount'])

        tmp['ppn'] = self._ppn
        tmp['doc_len'] = doc_len

        return tmp

    @staticmethod
    def initialize(voc, sqlite_file):

        CountJob.voc = voc
        CountJob.con = sqlite3.connect(sqlite_file)


def read_corpus(sqlite_file, processes, min_surface_len=2, min_proba=0.25, entities_file=None):

    entities = None
    if entities_file is not None:

        print("Reading id2work information from entities table ...")
        with sqlite3.connect(entities_file) as con:
            entities = pd.read_sql('SELECT * from entities', con=con).set_index('QID')

    with sqlite3.connect(sqlite_file) as con:

        print('Reading entity linking table ...')
        df = pd.read_sql('SELECT * from entity_linking', con=con).drop(columns=["index"]).reset_index(drop=True)
        print('done.')

        df = df.loc[(df.proba > min_proba) & (df.page_title.str.len() > min_surface_len)
                    & (df.entity_id.str.len() > min_surface_len + 4)]

        df = df.loc[df.wikidata.str.startswith('Q')]

        voc = {qid: i for i, qid in enumerate(df.wikidata.unique())}

        data = []

        def get_jobs():
            for ppn, part in tqdm(df.groupby('ppn')):
                yield CountJob(ppn, part)

        for i, tmp in enumerate(prun(get_jobs(), initializer=CountJob.initialize, initargs=(voc, sqlite_file), processes=processes)):

            data.append(tmp)

        data = pd.concat(data)

        if entities is not None:
            data = data.merge(entities[['label']], left_on='wikidata', right_index=True)

    return data, voc


@click.command()
@click.argument('sqlite-file', type=click.Path(exists=True), required=True, nargs=1)
@click.argument('corpus-file', type=click.Path(exists=False), required=True, nargs=1)
@click.option('--processes', default=4, help='Number of workers.')
@click.option('--min-proba', type=float, default=0.25, help='Minimum probability of counted entities.')
@click.option('--entities-file', default=None, help="Knowledge-base of entity linking step.")
def extract_corpus(sqlite_file, corpus_file, processes, min_proba, entities_file):

    data, voc = read_corpus(sqlite_file, processes=processes, min_proba=min_proba, entities_file=entities_file)

    data.to_pickle(corpus_file)


@click.command()
@click.argument('sqlite-file', type=click.Path(exists=True), required=True, nargs=1)
@click.argument('model-file', type=click.Path(exists=False), required=True, nargs=1)
@click.option('--num-topics', default=10, help='Number of topics in LDA topic model. Default 10.')
@click.option('--entities-file', default=None, help="Knowledge-base of entity linking step.")
@click.option('--processes', default=4, help='Number of workers.')
@click.option('--corpus-file', default=None, help="Write corpus to this file.")
@click.option('--min-proba', type=float, default=0.25, help='Minimum probability of counted entities.')
def run_lda(sqlite_file, model_file, num_topics, entities_file, processes, corpus_file, min_proba):
    """
    Reads entity linking data from SQLITE_FILE.
    Computes LDA-topic model and stores it in MODEL_FILE.
    """

    if corpus_file is None or not os.path.exists(corpus_file):
        data, voc = read_corpus(sqlite_file, processes=processes, entities_file=entities_file, min_proba=min_proba)
    else:
        data = pd.read_pickle(corpus_file)

    corpus, ppns = make_bow(data)

    print("Number of documents: {}.", len(corpus))

    if corpus_file is not None and not os.path.exists(corpus_file):
        print('Writing corpus to disk ...')

        data.to_pickle(corpus_file)

        print('done.')

    if 'label' in data.columns:
        data['label'] = data['wikidata'] + "(" + data['label'] + ")"
    else:
        data['label'] = data['wikidata']

    voc = data[['voc_index', 'label']].drop_duplicates().sort_values('voc_index').reset_index(drop=True)

    id2word = {int(voc_index): label for voc_index, label in zip(voc.voc_index.tolist(), voc.label.tolist())}

    print("Number of terms: {}.", len(voc))

    lda = LdaMulticore(corpus=corpus, num_topics=num_topics, id2word=id2word, workers=processes)

    lda.save(model_file)

