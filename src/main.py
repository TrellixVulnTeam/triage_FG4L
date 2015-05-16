#!/usr/bin/env python2
# -*- coding: utf-8 -*-

from __future__ import print_function

import logging
logger = logging.getLogger('cfl.main')

import sys
import os.path
import csv
import zipfile
import tarfile
from collections import namedtuple

import click
import dulwich.repo
import scipy.stats
import numpy
from gensim.corpora import MalletCorpus, Dictionary
from gensim.models import LdaModel, LsiModel
from gensim.matutils import sparse2full
from gensim.utils import smart_open

import utils
from corpora import (ChangesetCorpus, SnapshotCorpus, ReleaseCorpus,
                     TaserSnapshotCorpus, TaserReleaseCorpus,
                     CorpusCombiner, GeneralCorpus)
from errors import TaserError
from goldsets import build_developer_goldset


def error(*args, **kwargs):
    logger.error(*args)
    if 'errorno' in kwargs:
        sys.exit(kwargs['errorno'])

    sys.exit(1)

@click.command()
@click.option('--verbose', is_flag=True)
@click.option('--debug', is_flag=True)
@click.option('--force', is_flag=True)
@click.option('--temporal', is_flag=True)
@click.option('--goldset', is_flag=True)
@click.option('--lda', is_flag=True)
@click.option('--lsi', is_flag=True)
@click.option('--name', help="Name of project to run experiment on")
@click.option('--version', help="Version of project to run experiment on")
@click.option('--level', default="class", help="Granularity level of project to run experiment on")
@click.option('--num_topics', default=500, type=click.INT)
@click.option('--chunksize', default=2000, type=click.INT)
@click.option('--passes', default=5, type=click.INT)
@click.option('--iterations', default=1000, type=click.INT)
@click.option('--decay', default=0.5, type=click.FLOAT)
@click.option('--offset', default=1.0, type=click.FLOAT)
@click.option('--eta', type=click.FLOAT)
@click.option('--alpha', default='symmetric')
def cli(debug, verbose, name, version, goldset,  *args, **kwargs):
    logging.basicConfig(format='%(asctime)s : %(levelname)s : ' +
                        '%(name)s : %(funcName)s : %(message)s')

    if debug:
        logging.root.setLevel(level=logging.DEBUG)
    elif verbose:
        logging.root.setLevel(level=logging.INFO)
    else:
        logging.root.setLevel(level=logging.ERROR)

    # load project info
    projects = load_projects(kwargs)
    for project in projects:
        if name:
            name = name.lower()

            if name == project.name:
                if version and version != project.version:
                    continue

                if goldset:
                    build_developer_goldset(project)
                else:
                    run_experiment(project)
                sys.exit(0) # done, boom shakalaka
        elif goldset:
            build_developer_goldset(project)
        else:
            run_experiment(project)


def run_experiment(project):
    logger.info("Running project on %s", str(project))
    print(project)

    repos = load_repos(project)

    # create/load document lists
    queries = create_queries(project)
    goldsets = load_goldsets(project, 'committer')

    # get corpora
    changeset_corpus = create_corpus(project, repos, ChangesetCorpus, use_level=False)
    developer_corpus = create_developer_corpus(project, repos, changeset_corpus)
    release_corpus = create_release_corpus(project, repos)

    collect_info(project, repos, queries, goldsets, changeset_corpus, release_corpus)

    # release-based evaluation is #basic 💁
    release_lda, release_lsi = run_basic(project, release_corpus,
                                         release_corpus, queries, goldsets,
                                         'Release', use_level=True)

    changeset_lda, changeset_lsi = run_basic(project, changeset_corpus,
                                             release_corpus, queries, goldsets,
                                             'Changeset')

    if project.temporal:
        try:
            temporal_lda, temporal_lsi = run_temporal(project, repos,
                                                    changeset_corpus, queries,
                                                    goldsets)
        except IOError:
            logger.info("Files needed for temporal evaluation not found. Skipping.")
        else:
            if project.lda:
                do_science('temporal_lda', temporal_lda, changeset_lda, ignore=True)
            if project.lsi:
                do_science('temporal_lsi', temporal_lsi, changeset_lsi, ignore=True)

    # do this last so that the results are printed together
    if project.lda:
        do_science('basic_lda', changeset_lda, release_lda)
    if project.lsi:
        do_science('basic_lsi', changeset_lsi, release_lsi)

def collect_info(project, repos, queries, goldsets, changeset_corpus, release_corpus):
    logger.info("Collecting corpus metdata info")
    changeset_corpus.metadata = True
    release_corpus.metadata = True
    queries.metadata = True
    ids = set(i for i,g in goldsets)
    try:
        issue2git, git2issue = load_issue2git(project, ids)
    except:
        return

    path = os.path.join(project.full_path, "general-info.csv")
    if not os.path.exists(path):
        with smart_open(path, 'w') as f:
            writer = csv.writer(f)
            writer.writerow(["changesets_before_first"])
            row = list()

            for i, docmeta in enumerate(changeset_corpus):
                doc, meta = docmeta
                if meta[0] in git2issue:
                    row.append(i)
                    break

            writer.writerow(row)

    path = os.path.join(project.full_path, 'goldset-info.csv')
    if not os.path.exists(path):
        with smart_open(path, 'w') as f:
            writer = csv.writer(f)
            writer.writerow(["metadata", "total_entities"])

            for gid, goldset in goldsets:
                row = list()
                row.append(gid)
                row.append(len(goldset))
                writer.writerow(row)

    collect_helper(project, changeset_corpus, 'changeset')
    collect_helper(project, release_corpus, 'release' + project.level)
    collect_helper(project, queries, 'queries')

    changeset_corpus.metadata = False
    release_corpus.metadata = False
    queries.metadata = False

def collect_helper(project, corpus, name):
    logger.info("Helper corpus metdata info of " + name)
    path = os.path.join(project.full_path, '-'.join([name, 'info.csv']))

    if os.path.exists(path):
        return

    with smart_open(path, 'w') as f:
        writer = csv.writer(f)
        writer.writerow(["metadata", "unique_words", "total_words"])

        for doc, meta in corpus:
            row = list()
            row.append(meta[0])
            row.append(len(doc))
            row.append(sum(freq for word, freq in doc))
            writer.writerow(row)

def write_ranks(project, prefix, ranks):
    with smart_open(os.path.join(project.full_path, '-'.join([prefix, project.level, str(project.num_topics), 'ranks.csv.gz'])), 'w') as f:
        writer = csv.writer(f)
        writer.writerow(['id', 'rank', 'distance', 'item'])

        for gid, rl in ranks.items():
            for idx, rank in enumerate(rl):
                dist, meta = rank
                d_name, d_repo = meta
                writer.writerow([gid, idx+1, dist, d_name])

def read_ranks(project, prefix):
    ranks = dict()
    with smart_open(os.path.join(project.full_path, '-'.join([prefix, project.level, str(project.num_topics), 'ranks.csv.gz']))) as f:
        reader = csv.reader(f)
        next(reader)  # skip header
        for g_id, idx, dist, d_name in reader:
            if g_id not in ranks:
                ranks[g_id] = list()

            ranks[g_id].append( (dist, (d_name, 'x')) )

    return ranks


def run_basic(project, corpus, other_corpus, queries, goldsets, kind, use_level=False):
    """
    This function runs the experiment in one-shot. It does not evaluate the
    changesets over time.
    """
    logger.info("Running basic evaluation on the %s", kind)
    lda_first_rels = list()
    if project.lda:
        try:
            lda_ranks = read_ranks(project, kind.lower() + '_lda')
            logger.info("Sucessfully read previously written %s LDA ranks", kind)
            exists = True
        except IOError:
            exists = False

        if project.force or not exists:
            lda_model, _ = create_lda_model(project, corpus, corpus.id2word, kind, use_level=use_level)
            lda_query_topic = get_topics(lda_model, queries)
            lda_doc_topic = get_topics(lda_model, other_corpus)

            lda_ranks = get_rank(lda_query_topic, lda_doc_topic)
            write_ranks(project, kind.lower() + '_lda', lda_ranks)

        lda_first_rels = get_frms(goldsets, lda_ranks)

    lsi_first_rels = list()
    if project.lsi:
        try:
            lsi_ranks = read_ranks(project, kind.lower() + '_lsi')
            logger.info("Sucessfully read previously written %s LSI ranks", kind)
            exists = True
        except IOError:
            exists = False

        if project.force or not exists:
            lsi_model, _ = create_lsi_model(project, corpus, corpus.id2word, kind, use_level=use_level)
            lsi_query_topic = get_topics(lsi_model, queries)
            lsi_doc_topic = get_topics(lsi_model, other_corpus)

            lsi_ranks = get_rank(lsi_query_topic, lsi_doc_topic)
            write_ranks(project, kind.lower() + '_lsi', lsi_ranks)

        lsi_first_rels = get_frms(goldsets, lsi_ranks)

    return lda_first_rels, lsi_first_rels

def run_temporal(project, repos, corpus, queries, goldsets):
    logger.info("Running temporal evaluation")

    force = project.force
    lda_rels = list()
    lsi_rels = list()
    try:
        if project.lda:
            lda_ranks = read_ranks(project, 'temporal')
            lda_rels = get_frms(goldsets, lda_ranks)

        if project.lsi:
            lsi_ranks = read_ranks(project, 'temporal_lsi')
            lsi_rels = get_frms(goldsets, lsi_ranks)

        logger.info("Sucessfully read previously written Temporal ranks")
    except IOError:
        force = True

    if force:
        lda_rels, lsi_rels = run_temporal_helper(project, repos, corpus, queries, goldsets)

    return lda_rels, lsi_rels


def run_temporal_helper(project, repos, corpus, queries, goldsets):
    """
    This function runs the experiment in over time. That is, it stops whenever
    it reaches a commit linked with an issue/query. Will not work on all
    projects.
    """
    ids = set(i for i,g in goldsets)
    issue2git, git2issue = load_issue2git(project, ids)

    logger.info("Stopping at %d commits for %d issues", len(git2issue), len(issue2git))

    if project.lda:
        lda, lda_fname = create_lda_model(project, None, corpus.id2word,
                                        'Temporal', use_level=False, force=True)

    if project.lsi:
        lsi, lsi_fname = create_lsi_model(project, None, corpus.id2word,
                                        'Temporal', use_level=False, force=True)

    indices = list()
    lda_ranks = dict()
    lsi_ranks = dict()
    docs = list()
    corpus.metadata = True
    prev = 0

    # let's partition the corpus first
    for idx, docmeta in enumerate(corpus):
        doc, meta = docmeta
        sha, _ = meta
        if sha in git2issue:
            indices.append((prev, idx+1, sha))
            prev = idx

    logger.info('Created %d partitions of the corpus', len(indices))
    corpus.metadata = False

    for counter, index  in enumerate(indices):
        logger.info('At %d of %d partitions', counter, len(indices))
        start, end, sha = index
        docs = list()
        for i in xrange(start, end):
            docs.append(corpus[i])

        if project.lda:
            lda.update(docs,
                       #chunksize=project.chunksize,
                       offset=project.offset,
                       decay=project.decay)
        if project.lsi:
            lsi.add_documents(docs)

        for qid in git2issue[sha]:
            logger.info('Getting ranks for query id %s', qid)
            # build a snapshot corpus of items *at this commit*
            try:
                other_corpus = create_release_corpus(project, repos, forced_ref=sha)
            except TaserError:
                continue

            # do LDA magic
            if project.lda:
                lda_query_topic = get_topics(lda, queries, by_ids=[qid])
                lda_doc_topic = get_topics(lda, other_corpus)
                lda_subranks = get_rank(lda_query_topic, lda_doc_topic)
                if qid in lda_subranks:
                    if qid not in lda_ranks:
                        lda_ranks[qid] = list()

                    rank = lda_subranks[qid]
                    lda_ranks[qid].extend(rank)
                else:
                    logger.info('Couldnt find qid %s', qid)

            # do LSI magic
            if project.lsi:
                lsi_query_topic = get_topics(lsi, queries, by_ids=[qid])
                lsi_doc_topic = get_topics(lsi, other_corpus)
                lsi_subranks = get_rank(lsi_query_topic, lsi_doc_topic)
                if qid in lsi_subranks:
                    if qid not in lsi_ranks:
                        lsi_ranks[qid] = list()

                    rank = lsi_subranks[qid]
                    lsi_ranks[qid].extend(rank)
                else:
                    logger.info('Couldnt find qid %s', qid)

    lda_rels = list()
    if project.lda:
        lda.save(lda_fname)
        write_ranks(project, 'temporal', lda_ranks)
        lda_rels = get_frms(goldsets, lda_ranks)

    lsi_rels = list()
    if project.lsi:
        lsi.save(lsi_fname)
        write_ranks(project, 'temporal_lsi', lsi_ranks)
        lsi_rels = get_frms(goldsets, lsi_ranks)

    return lda_rels, lsi_rels


def merge_first_rels(a, b, ignore=False):
    first_rels = dict()

    for num, query_id, doc_meta in a:
        qid = int(query_id)
        if qid not in first_rels:
            first_rels[qid] = [num]
        else:
            logger.info('duplicate qid found: %s', query_id)

    for num, query_id, doc_meta in b:
        qid = int(query_id)
        if qid not in first_rels and not ignore:
            logger.info('qid not found: %s', qid)
            first_rels[qid] = [0]

        if qid in first_rels:
            first_rels[qid].append(num)

    for key, v in first_rels.items():
        if len(v) == 1:
            v.append(0)

    x = [v[0] for v in first_rels.values()]
    y = [v[1] for v in first_rels.values()]
    return x, y

def do_science(prefix, changeset_first_rels, release_first_rels, ignore=False):
    # Build a dictionary with each of the results for stats.
    x, y = merge_first_rels(changeset_first_rels, release_first_rels, ignore=ignore)

    print(prefix+' changeset mrr:', utils.calculate_mrr(x))
    print(prefix+' release mrr:', utils.calculate_mrr(y))
    print(prefix+' wilcoxon signedrank:', scipy.stats.wilcoxon(x, y))
    #print(prefix+' ranksums:', scipy.stats.ranksums(x, y))
    #print(prefix+' mann-whitney:', scipy.stats.mannwhitneyu(x, y))
    #print('friedman:', scipy.stats.friedmanchisquare(x, y, x2, y2))


def get_frms(goldsets, ranks):
    logger.info('Getting FRMS for %d goldsets and %d ranks',
                len(goldsets), len(ranks))
    frms = list()

    for g_id, goldset in goldsets:
        if g_id not in ranks:
            logger.info('Could not find ranks for goldset id %s', g_id)
        else:
            logger.debug('Getting best rank out of %d shas', len(ranks[g_id]))
            subfrms = list()
            for idx, rank in enumerate(ranks[g_id]):
                dist, meta = rank
                d_name, d_repo = meta
                if d_name in goldset:
                    subfrms.append((idx+1, int(g_id), d_name))
                    break

            # i think this might be cheating? :)
            subfrms.sort()
            logger.debug(str(subfrms))
            if subfrms:
                frms.append(subfrms[0])


    logger.info('Returning %d FRMS', len(frms))
    return frms


def get_rank(query_topic, doc_topic, distance_measure=utils.hellinger_distance):
    logger.info('Getting ranks between %d query topics and %d doc topics',
                len(query_topic), len(doc_topic))
    ranks = dict()
    for q_meta, query in query_topic:
        qid, _ = q_meta
        q_dist = list()

        for d_meta, doc in doc_topic:
            distance = distance_measure(query, doc)
            q_dist.append((distance, d_meta))

        if qid in ranks:
            raise Exception("WHAT")

        ranks[qid] = list(sorted(q_dist))

    logger.info('Returning %d ranks', len(ranks))
    return ranks


def get_topics(model, corpus, by_ids=None, full=True):
    logger.info('Getting doc topic for corpus with length %d', len(corpus))
    doc_topic = list()
    corpus.metadata = True
    old_id2word = corpus.id2word
    corpus.id2word = model.id2word

    for doc, metadata in corpus:
        if by_ids is None or metadata[0] in by_ids:
            # get a vector where low topic values are zeroed out.
            topics = model[doc]
            if full:
                topics = sparse2full(topics, model.num_topics)

            # this gets the "full" vector that includes low topic values
            # topics = model.__getitem__(doc, eps=0)
            # topics = [val for id, val in topics]

            doc_topic.append((metadata, topics))

    corpus.metadata = False
    corpus.id2word = old_id2word
    logger.info('Returning doc topic of length %d', len(doc_topic))

    return doc_topic


def load_goldsets(project, kind):
    logger.info("Loading goldsets for project: %s", str(project))
    with open(os.path.join(project.full_path, 'ids.txt')) as f:
        ids = [x.strip() for x in f.readlines()]

    goldsets = list()
    s = 0
    for id in ids:
        try:
            with open(os.path.join(project.full_path, 'goldsets', kind,
                                    id + '.txt')) as f:
                golds = frozenset(x.strip() for x in f.readlines())
                s += len(golds)

            goldsets.append((id, golds))
        except IOError:
            pass

    logger.info("Returning %d goldsets %d", len(goldsets), s)
    return goldsets


def load_issue2git(project, ids):
    dest_fn = os.path.join(project.full_path, 'issue2git.csv')
    if os.path.exists(dest_fn):
        write_out = False
        i2g = dict()
        with open(dest_fn) as f:
            r = csv.reader(f)
            for row in r:
                i2g[row[0]] = row[1:]
    else:
        write_out = True

        i2s = dict()
        fn = os.path.join(project.full_path, 'IssuesToSVNCommitsMapping.txt')
        with open(fn) as f:
            lines = [line.strip().split('\t') for line in f]
            for line in lines:
                issue = line[0]
                links = line[1]
                svns = line[2:]

                i2s[issue] = svns

        s2g = dict()
        fn = os.path.join(project.data_path, 'svn2git.csv')
        with open(fn) as f:
            reader = csv.reader(f)
            for svn,git in reader:
                if svn in s2g and s2g[svn] != git:
                    logger.info('Different gits sha for SVN revision number %s', svn)
                else:
                    s2g[svn] = git

        i2g = dict()
        for issue, svns in i2s.items():
            for svn in svns:
                if svn in s2g:
                    # make sure we don't have issues that are empty
                    if issue not in i2g:
                        i2g[issue] = list()
                    i2g[issue].append(s2g[svn])
                else:
                    logger.info('Could not find git sha for SVN revision number %s', svn)

    # Make sure we have a commit for all issues
    keys = set(i2g.keys())
    ignore = ids - keys
    if len(ignore):
        logger.info("Ignoring evaluation for the following issues:\n\t%s",
                    '\n\t'.join(ignore))

    # clean up issue2git
    for issue in i2g.keys():
        if issue in ignore or issue not in ids:
            i2g.pop(issue)

    # build reverse mapping
    g2i = dict()
    for issue, gits in i2g.items():
        for git in gits:
            if git not in g2i:
                g2i[git] = list()
            g2i[git].append(issue)

    if write_out:
        with open(dest_fn, 'w') as f:
            w = csv.writer(f)
            for issue, gits in i2g.items():
                w.writerow([issue] + gits)

    return i2g, g2i


def load_projects(config):
    projects = list()
    with open("projects.csv", 'r') as f:
        reader = csv.reader(f)
        header = next(reader)
        customs = ['data_path', 'full_path', 'src_path']
        Project = namedtuple('Project',  ' '.join(header + customs + config.keys()))
        # figure out which column index contains the project name
        name_idx = header.index("name")
        version_idx = header.index("version")

        # find the project in the csv, adding it's info to config
        for row in reader:
            # built the data_path value
            row += (os.path.join('data', row[name_idx], ''),)

            # build the full_path value
            row += (os.path.join('data', row[name_idx], row[version_idx], ''),)

            # build the src_path value
            row += (os.path.join('data', row[name_idx], row[version_idx], 'src'),)

            row += config.values()

            projects.append(Project(*row))

    return projects


def load_repos(project):
    # reading in repos
    with open(os.path.join('data', project.name, 'repos.txt')) as f:
        repo_urls = [line.strip() for line in f]

    repos_base = 'gits'
    if not os.path.exists(repos_base):
        utils.mkdir(repos_base)

    repos = list()

    for url in repo_urls:
        repo_name = url.split('/')[-1]
        target = os.path.join(repos_base, repo_name)
        try:
            repo = utils.clone(url, target, bare=True)
        except OSError:
            repo = dulwich.repo.Repo(target)

        repos.append(repo)

    return repos


def create_queries(project):
    corpus_fname_base = project.full_path + 'Queries'
    corpus_fname = corpus_fname_base + '.mallet.gz'
    dict_fname = corpus_fname_base + '.dict.gz'

    if not os.path.exists(corpus_fname):
        pp = GeneralCorpus(lazy_dict=True)
        id2word = Dictionary()

        with open(os.path.join(project.full_path, 'ids.txt')) as f:
            ids = [x.strip() for x in f.readlines()]

        queries = list()
        for id in ids:
            with open(os.path.join(project.full_path, 'queries',
                                    'ShortDescription' + id + '.txt')) as f:
                short = f.read()

            with open(os.path.join(project.full_path, 'queries',
                                    'LongDescription' + id + '.txt')) as f:
                long = f.read()

            text = ' '.join([short, long])
            text = pp.preprocess(text)

            # this step will remove any words not found in the dictionary
            bow = id2word.doc2bow(text, allow_update=True)

            queries.append((bow, (id, 'query')))

        # write the corpus and dictionary to disk. this will take awhile.
        MalletCorpus.serialize(corpus_fname, queries, id2word=id2word,
                               metadata=True)

    # re-open the compressed versions of the dictionary and corpus
    id2word = None
    if os.path.exists(dict_fname):
        id2word = Dictionary.load(dict_fname)

    corpus = MalletCorpus(corpus_fname, id2word=id2word)

    return corpus


def create_lda_model(project, corpus, id2word, name, use_level=True, force=False):
    model_fname = project.full_path + name + str(project.num_topics)
    if use_level:
        model_fname += project.level

    model_fname += '.lda.gz'


    if not os.path.exists(model_fname) or project.force or force:
        if corpus:
            update_every=None # run in batch if we have a pre-supplied corpus
        else:
            update_every=1

        model = LdaModel(corpus=corpus,
                         id2word=id2word,
                         alpha=project.alpha,
                         eta=project.eta,
                         chunksize=project.chunksize,
                         passes=project.passes,
                         num_topics=project.num_topics,
                         iterations=project.iterations,
                         eval_every=None, # disable perplexity tests for speed
                         update_every=update_every,
                         )

        if corpus:
            model.save(model_fname)
    else:
        model = LdaModel.load(model_fname)

    return model, model_fname

def create_lsi_model(project, corpus, id2word, name, use_level=True, force=False):
    model_fname = project.full_path + name + str(project.num_topics)
    if use_level:
        model_fname += project.level

    model_fname += '.lsi.gz'

    if not os.path.exists(model_fname) or project.force or force:
        model = LsiModel(corpus=corpus,
                         id2word=id2word,
                         num_topics=project.num_topics,
                         )

        if corpus:
            model.save(model_fname)
    else:
        model = LsiModel.load(model_fname)

    return model, model_fname


def create_mallet_model(project, corpus, name, use_level=True):
    model_fname = project.full_path + name + str(project.num_topics)
    if use_level:
        model_fname += project.level

    model_fname += '.malletlda.gz'

    if not os.path.exists(model_fname):
        model = LdaMallet('./lib/mallet-2.0.7/bin/mallet',
                          corpus=corpus,
                          id2word=corpus.id2word,
                          optimize_interval=10,
                          num_topics=project.num_topics)

        model.save(model_fname)
    else:
        model = LdaMallet.load(model_fname)

    return model

def create_developer_corpus(project, repos, changesets):
    corpus_fname_base = project.full_path + 'Developer'

    corpus_fname = corpus_fname_base + '.mallet.gz'
    dict_fname = corpus_fname_base + '.dict.gz'
    made_one = False

    dev_words = dict()
    if not os.path.exists(corpus_fname):
        changesets.metadata = True
        for doc, meta in changesets:
            cid, label = meta

            commit = None
            for repo in repos:
                if cid in repo:
                    commit = repo[str(cid)]
                    break

            if commit:
                dev = commit.committer
                dev = dev.replace(" ", "_") # mallet cant have spaces in ids
                if dev not in dev_words:
                    dev_words[dev] = list()

                dev_words[dev].extend(doc)


        changesets.metadata = False
        dev_words = [(v, (k, 'dev')) for k, v in dev_words.items()]

        MalletCorpus.serialize(corpus_fname, dev_words, id2word=changesets.id2word,
                               metadata=True)

        changesets.id2word.save(dict_fname)

    id2word = None
    if os.path.exists(dict_fname):
        id2word = Dictionary.load(dict_fname)

    corpus = MalletCorpus(corpus_fname, id2word=id2word)

    return corpus


def create_corpus(project, repos, Kind, use_level=True, forced_ref=None):
    corpus_fname_base = project.full_path + Kind.__name__

    if use_level:
        corpus_fname_base += project.level

    if forced_ref:
        corpus_fname_base += forced_ref[:8]

    corpus_fname = corpus_fname_base + '.mallet.gz'
    dict_fname = corpus_fname_base + '.dict.gz'
    made_one = False

    if not os.path.exists(corpus_fname):
        combiner = CorpusCombiner()

        for repo in repos:
            try:
                if repo or forced_ref:
                    corpus = Kind(project=project,
                                  repo=repo,
                                  lazy_dict=True,
                                  ref=forced_ref,
                                  )
                else:
                    corpus = Kind(project=project, lazy_dict=True)

            except KeyError:
                continue
            except TaserError as e:
                if repo == repos[-1] and not made_one:
                    raise e
                    # basically, if we are at the last repo and we STILL
                    # haven't sucessfully extracted a corpus, ring some bells
                else:
                    # otherwise, keep trying. winners never quit.
                    continue

            combiner.add(corpus)
            made_one = True

        # write the corpus and dictionary to disk. this will take awhile.
        combiner.metadata = True
        MalletCorpus.serialize(corpus_fname, combiner, id2word=combiner.id2word,
                               metadata=True)
        combiner.metadata = False

        # write out the dictionary
        combiner.id2word.save(dict_fname)

    # re-open the compressed versions of the dictionary and corpus
    id2word = None
    if os.path.exists(dict_fname):
        id2word = Dictionary.load(dict_fname)

    corpus = MalletCorpus(corpus_fname, id2word=id2word)

    return corpus


def create_release_corpus(project, repos, forced_ref=None):
    if project.level == 'file':
        RC = ReleaseCorpus
        SC = SnapshotCorpus
    else:
        RC = TaserReleaseCorpus
        SC = TaserSnapshotCorpus

    return create_corpus(project, repos, SC, forced_ref=forced_ref)

    # not using this just yet
    if forced_ref:
        return create_corpus(project, repos, SC, forced_ref=forced_ref)
    else:
        try:
            return create_corpus(project, [None], RC)
        except TaserError:
            return create_corpus(project, repos, SC, forced_ref=forced_ref)

