import yaml
import pandas as pd
import numpy as np
from os.path import join
import glob
import sys


DIRECTIONS = ['R1', 'R2']

def parse_samplesheet(fp_samplesheet):
    ss = pd.read_csv(fp_samplesheet, sep=",", skiprows=21, dtype={'Sample_Name': str, 'Sample_ID': str})

    # bcl2fasta automatically changes - into _ char in output filenames
    for f in ['Sample_ID', 'Sample_Name', 'Sample_Project']:
        ss[f] = ss[f].apply(lambda x: x.replace('-', '_') if type(x) != float else x)

    # bcl2fastq uses a S%03i index to address samples.
    # They are numbered as occuring in the samplesheet order starting with 1.
    # However, number is not increased if Sample_ID was already seen.
    uidx = dict()
    for _, sample_id in ss['Sample_ID'].iteritems():
        if sample_id not in uidx:
            uidx[sample_id] = len(uidx) + 1
    ss['s-idx'] = ss['Sample_ID'].apply(lambda x: uidx[x])

    # TODO: ensure that sample names do not clash when not considering s-idx!

    # fastq-prefix
    fp_fastqs = []
    for idx, row in ss.iterrows():
        fp_fastq = ''
        if pd.notnull(row['Sample_Project']):
            fp_fastq = row['Sample_Project']
        if pd.notnull(row['Sample_Name']):
            fp_fastq = join(fp_fastq, row['Sample_ID'])
        fp_fastqs.append(join(fp_fastq,
            '%s' % (
                row['Sample_Name'] if pd.notnull(
                    row['Sample_Name']) else row['Sample_ID'])))
    ss['fastq-prefix'] = fp_fastqs

    return ss


def get_global_samplesheets(dir_samplesheets):
    # parse all available sample sheets
    fps_samplesheets = glob.glob('%s*XX_ukd.csv' % dir_samplesheets)

    global_samplesheet = []
    for fp_samplesheet in fps_samplesheets:
        ss = parse_samplesheet(fp_samplesheet)
        ss['run'] = fp_samplesheet.split('/')[-1].replace('_ukd.csv', '')
        global_samplesheet.append(ss)
    global_samplesheet = pd.concat(global_samplesheet, sort=False)

    return global_samplesheet


def get_role(ukd_project, ukd_entity_id, ukd_entity_role, samplesheets):
    """Returns file path for bam, given project, entity and role (for trio).

    Parameters
    ----------
    ukd_project : str
        Name of project, to avoid entity ID clashes across projects.
    ukd_entity_id : str
        Entity ID for which role needs to be obtained.
    ukd_entity_role : str
        Role of entity ID whose bam filepath shall be returned.
    config : snakemake.config
        Snakemakes config object to obtain file path of sample sheets.

    Returns
    -------
    str: Filepath of bam file for given entity role.
    """
    samples = samplesheets

    # select correct project
    try:
        x = samples[samples['Sample_Project'] == ukd_project]
        x.iloc[0]
    except IndexError:
        raise ValueError('Could not find an UKD project with name "%s". Available projects are:\n\t%s\n' % (ukd_project, '\n\t'.join(sorted(samples['Sample_Project'].unique()))))
    else:
        samples = x

    # select correct entity
    try:
        x = samples[samples['ukd_entity_id'] == ukd_entity_id]
        x.iloc[0]
    except IndexError:
        raise ValueError('Could not find an UKD entity group with name "%s". Available entities for projects "%s" are:\n\t%s\n' % (ukd_entity_id, ukd_project, '\n\t'.join(sorted(samples['ukd_entity_id'].unique()))))
    else:
        samples = x

    # select correct role
    try:
        x = samples[samples['ukd_entity_role'] == ukd_entity_role]
        x.iloc[0]
    except IndexError:
        raise ValueError('Could not find a role "%s" for UKD entity group with name "%s". Available roles are:\n\t%s\n' % (ukd_entity_role, ukd_entity_id, '\n\t'.join(sorted(samples['ukd_entity_role'].unique()))))
    else:
        samples = x

    res = {sample['fastq-prefix'] for idx, sample in samples.iterrows()}
    if len(res) > 1:
        raise ValueError("Stefan, check if use cases can occour with more than one result!")

    return list(res)[0]


def get_species(sample, samplesheets, config):
    # sample can be a single sample ...
    projects = samplesheets[samplesheets['fastq-prefix'] == sample]['Sample_Project'].unique()

    # ... or an entity
    if len(projects) == 0:
        projects = samplesheets[(samplesheets['Sample_Project'] == sample.split('/')[0]) & (samplesheets['ukd_entity_id'] == sample.split('/')[-1])]['Sample_Project'].unique()

    if len(projects) > 1:
        raise ValueError("Ambiguous projects: '%s' for sample '%s'" % (projects, sample))

    return config['projects'][projects[0]]['species']


def get_reference_genome(sample, samplesheets, config):
    return config['references']['genomes'][get_species(sample, samplesheets, config)]


def get_reference_knowns(sample, samplesheets, config, _key):
    return [k for k in config['references']['knowns'][get_species(sample, samplesheets, config)] if _key in k]


def get_reference_exometrack(sample, samplesheets, config):
    return config['references']['exometrack'][get_species(sample, samplesheets, config)]['file']


def get_reference_varscan_somatic(sample, samplesheets, config):
    return config['references']['varscan_somatic'][get_species(sample, samplesheets, config)]


######## avoid run
def _run2date(run):
    date='%04i/%02i/%02i' % (
        int(run.split('_')[0][:2])+2000,
        int(run.split('_')[0][3:4]),
        int(run.split('_')[0][5:6]))
    return date


def get_bwa_mem_header(sample, samplesheets, config):
    samples = samplesheets[samplesheets['fastq-prefix'] == sample]
    res = ' -R "@RG\\tID:%s\\tCN:Department_of_Pediatric_Oncology_Dusseldorf\\tPU:%s\\tDT:%s\\tPL:ILLUMINA\\tLB:%s\\tSM:readgroups.info"' % (
        ' and '.join(samples['run'].dropna().unique()),
        ' and '.join(list(map(lambda x: x.split('_')[-1][1:], samples['run'].dropna().unique()))),
        ' and '.join(list(map(_run2date, samples['run'].dropna().unique()))),
        config['references']['exometrack'][get_species(sample, samplesheets, config)]['protocol_name']
        )
    return res


def get_demux_samples(samplesheets, config):
    # get projects that require snv vs. reference analysis
    background_projects = [prj_name for prj_name in config['projects'] if 'demultiplex' in config['projects'][prj_name]['actions']]

    # filter samples to those belonging to tumor vs. normal projects
    background_samples = samplesheets[samplesheets['Sample_Project'].isin(background_projects)]

    return list(background_samples['run'].unique())
    # samples = []
    # for _, sample in background_samples.iterrows():
    #     samples.extend(['%s/%s_L%03i_%s_001.fastq.gz' % (sample['run'], sample['fastq-prefix'], sample['Lane'], direction) for direction in config['directions']])
    #
    # return samples


def get_samples(samplesheets, config):
    # get projects that require snv vs. reference analysis
    background_projects = [prj_name for prj_name in config['projects'] if 'background' in config['projects'][prj_name]['actions']]

    # filter samples to those belonging to tumor vs. normal projects
    background_samples = samplesheets[samplesheets['Sample_Project'].isin(background_projects)]

    samples = []
    for sample, g in background_samples.groupby(['Sample_Project', 'fastq-prefix']):
        samples.append({'Sample_Project': sample[0],
                        'sample': sample[1]})

    return samples


def get_tumorNormalPairs(samplesheets, config):
    # get projects that require tumor vs. normal analysis
    tumornormal_projects = [prj_name for prj_name in config['projects'] if 'tumornormal' in config['projects'][prj_name]['actions']]

    # filter samples to those belonging to tumor vs. normal projects
    tumornormal_samples = samplesheets[samplesheets['Sample_Project'].isin(tumornormal_projects)]

    pairs = []
    for pair, g in tumornormal_samples.groupby(['Sample_Project', 'ukd_entity_id']):
        # only choose comlete pairs
        if set(g['ukd_entity_role'].unique()) == {'healthy','tumor'}:
            pairs.append({'Sample_Project': pair[0],
                          'ukd_entity_id': pair[1]})

    return pairs


def get_trios(samplesheets, config):
    # get projects that require trio analysis
    trio_projects = [prj_name for prj_name in config['projects'] if 'trio' in config['projects'][prj_name]['actions']]

    # filter samples to those belonging to trio projects
    trio_samples = samplesheets[samplesheets['Sample_Project'].isin(trio_projects)]

    trios = []
    for trio, g in trio_samples.groupby(['Sample_Project', 'ukd_entity_id']):
        # only choose comlete trios
        if set(g['ukd_entity_role'].unique()) == {'patient', 'mother', 'father'}:
            trios.append({'Sample_Project': trio[0],
                          'ukd_entity_id': trio[1]})

    return trios


def get_projects_with_exomecoverage(config):
    res = []
    for name in config['projects']:
        if ('actions' in config['projects'][name]) and (len(set(config['projects'][name]['actions']) & set(['background', 'trio', 'tumornormal'])) > 0):
            res.append(name)
    return res


def get_rejoin_fastqs(sample, samplesheets, config):
    res = []
    for _, row in samplesheets[samplesheets['fastq-prefix'] == sample].iterrows():
        res.append('%s/%s_L%03i' % (row['run'], row['fastq-prefix'], row['Lane']))
    return res


def get_xenograft_hybridreference(sample, samplesheets, config):
    project = samplesheets[samplesheets['fastq-prefix'] == sample]['Sample_Project'].dropna().unique()
    if len(project) != 1:
        raise ValueError("_get_reference: Sample '%s' has ambiguous or missing project!" % sample)
    return config['projects'][project[0]]['xenograft']


def get_xenograft_stepname(sample, samplesheets, config):
    project = samplesheets[samplesheets['fastq-prefix'] == sample]['Sample_Project'].dropna().unique()
    if len(project) != 1:
        sys.stderr.write('%s\n' % (project))
        raise ValueError("_get_stepname: Sample '%s' has ambiguous or missing project!" % sample)
    if 'xenograft' in config['projects'][project[0]] and config['projects'][project[0]]['xenograft'] != "":
        return config['stepnames']['xenograft_bwa_sampe']
    else:
        return config['stepnames']['trim']
