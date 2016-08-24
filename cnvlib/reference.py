"""Supporting functions for the 'reference' command."""
from __future__ import absolute_import, division, print_function
from builtins import map, zip

import logging

import numpy as np

from . import core, fix, descriptives, ngfrills, params, tabio
from .cnary import CopyNumArray as CNA


def bed2probes(bed_fname):
    """Create neutral-coverage probes from intervals."""
    regions = tabio.read_auto(bed_fname)
    table = regions.data.loc[:, ("chromosome", "start", "end")]
    table["gene"] = (regions.data["gene"] if "gene" in regions.data else '-')
    table["log2"] = 0.0
    table["spread"] = 0.0
    return CNA(table, {"sample_id": core.fbase(bed_fname)})


def combine_probes(filenames, fa_fname, is_male_reference, skip_low,
                   fix_gc, fix_edge, fix_rmask):
    """Calculate the median coverage of each bin across multiple samples.

    Input:
        List of .cnn files, as generated by 'coverage' or 'import-picard'.
        `fa_fname`: fil columns for GC and RepeatMasker genomic values.
    Returns:
        A single CopyNumArray summarizing the coverages of the input samples,
        including each bin's "average" coverage, "spread" of coverages, and
        genomic GC content.
    """
    columns = {}

    # Load coverage from target/antitarget files
    logging.info("Loading %s", filenames[0])
    cnarr1 = tabio.read_cna(filenames[0])
    if not len(cnarr1):
        # Just create an empty array with the right columns
        col_names = ['chromosome', 'start', 'end', 'gene', 'log2', 'depth']
        if 'gc' in cnarr1 or fa_fname:
            col_names.append('gc')
        if fa_fname:
            col_names.append('rmask')
        col_names.append('spread')
        return CNA.from_rows([], col_names, {'sample_id': "reference"})

    # Calculate GC and RepeatMasker content for each probe's genomic region
    if fa_fname and (fix_rmask or fix_gc):
        gc, rmask = get_fasta_stats(cnarr1, fa_fname)
        if fix_gc:
            columns['gc'] = gc
        if fix_rmask:
            columns['rmask'] = rmask
    elif 'gc' in cnarr1 and fix_gc:
        # Reuse .cnn GC values if they're already stored (via import-picard)
        gc = cnarr1['gc']
        columns['gc'] = gc

    # Make the sex-chromosome coverages of male and female samples compatible
    is_chr_x = (cnarr1.chromosome == cnarr1._chr_x_label)
    is_chr_y = (cnarr1.chromosome == cnarr1._chr_y_label)
    flat_coverage = cnarr1.expect_flat_log2(is_male_reference)
    def shift_sex_chroms(cnarr):
        """Shift sample X and Y chromosomes to match the reference gender.

        Reference values:
            XY: chrX -1, chrY -1
            XX: chrX 0, chrY -1

        Plan:
          chrX:
            xx sample, xx ref: 0    (from 0)
            xx sample, xy ref: -= 1 (from -1)
            xy sample, xx ref: += 1 (from 0)    +1
            xy sample, xy ref: 0    (from -1)   +1
          chrY:
            xx sample, xx ref: = -1 (from -1)
            xx sample, xy ref: = -1 (from -1)
            xy sample, xx ref: 0    (from -1)   +1
            xy sample, xy ref: 0    (from -1)   +1

        """
        is_sample_female = cnarr.guess_xx()
        cnarr['log2'] += flat_coverage
        if is_sample_female:
            # chrX already OK
            # No chrY; it's all noise, so just match the male
            cnarr[is_chr_y, 'log2'] = -1.0
        else:
            # 1/2 #copies of each sex chromosome
            cnarr[is_chr_x | is_chr_y, 'log2'] += 1.0

    edge_bias = fix.get_edge_bias(cnarr1, params.INSERT_SIZE)
    def bias_correct_coverage(cnarr):
        """Perform bias corrections on the sample."""
        cnarr.center_all(skip_low=skip_low)
        shift_sex_chroms(cnarr)
        # Skip bias corrections if most bins have no coverage (e.g. user error)
        if (cnarr['log2'] > params.NULL_LOG2_COVERAGE - params.MIN_REF_COVERAGE
           ).sum() <= len(cnarr) // 2:
            logging.warn("WARNING: most bins have no or very low coverage; "
                         "check that the right BED file was used")
        else:
            if 'gc' in columns and fix_gc:
                logging.info("Correcting for GC bias...")
                cnarr = fix.center_by_window(cnarr, .1, columns['gc'])
            if 'rmask' in columns and fix_rmask:
                logging.info("Correcting for RepeatMasker bias...")
                cnarr = fix.center_by_window(cnarr, .1, columns['rmask'])
            if fix_edge:
                logging.info("Correcting for density bias...")
                cnarr = fix.center_by_window(cnarr, .1, edge_bias)
        return cnarr['log2']

    # Pseudocount of 1 "flat" sample
    all_coverages = [flat_coverage, bias_correct_coverage(cnarr1)]
    all_depths = [cnarr1['depth'] if 'depth' in cnarr1
                  else np.exp2(cnarr1['log2'])]
    for fname in filenames[1:]:
        logging.info("Loading target %s", fname)
        cnarrx = tabio.read_cna(fname)
        # Bin information should match across all files
        if not (len(cnarr1) == len(cnarrx)
                and (cnarr1.chromosome == cnarrx.chromosome).all()
                and (cnarr1.start == cnarrx.start).all()
                and (cnarr1.end == cnarrx.end).all()
                and (cnarr1['gene'] == cnarrx['gene']).all()):
            raise RuntimeError("%s probes do not match those in %s"
                               % (fname, filenames[0]))
        all_coverages.append(bias_correct_coverage(cnarrx))
        all_depths.append(cnarrx['depth'] if 'depth' in cnarrx
                          else np.exp2(cnarrx['log2']))
    all_coverages = np.vstack(all_coverages)

    logging.info("Calculating average bin coverages")
    cvg_centers = np.apply_along_axis(descriptives.biweight_location, 0,
                                      all_coverages)
    depth_centers = np.apply_along_axis(descriptives.biweight_location, 0,
                                        np.vstack(all_depths))
    logging.info("Calculating bin spreads")
    spreads = np.asarray([descriptives.biweight_midvariance(a, initial=i)
                          for a, i in zip(all_coverages.T, cvg_centers)])
    columns.update({
        'chromosome': cnarr1.chromosome,
        'start': cnarr1.start,
        'end': cnarr1.end,
        'gene': cnarr1['gene'],
        'log2': cvg_centers,
        'depth': depth_centers,
        'spread': spreads,
    })
    return CNA.from_columns(columns, {'sample_id': "reference"})


def warn_bad_probes(probes):
    """Warn about target probes where coverage is poor.

    Prints a formatted table to stderr.
    """
    bad_probes = probes[fix.mask_bad_bins(probes)]
    fg_index = (bad_probes['gene'] != 'Background')
    fg_bad_probes = bad_probes[fg_index]
    if len(fg_bad_probes) > 0:
        bad_pct = 100 * len(fg_bad_probes) / sum(probes['gene'] != 'Background')
        logging.info("Targets: %d (%s) bins failed filters:",
                     len(fg_bad_probes), "%.4f" % bad_pct + '%')
        gene_cols = max(list(map(len, fg_bad_probes['gene'])))
        labels = list(map(CNA.row2label, fg_bad_probes))
        chrom_cols = max(list(map(len, labels)))
        last_gene = None
        for label, probe in zip(labels, fg_bad_probes):
            if probe.gene == last_gene:
                gene = '  "'
            else:
                gene = probe.gene
                last_gene = gene
            if 'rmask' in probes:
                logging.info("  %s  %s  log2=%.3f  spread=%.3f  rmask=%.3f",
                             gene.ljust(gene_cols), label.ljust(chrom_cols),
                             probe.log2, probe.spread, probe.rmask)
            else:
                logging.info("  %s  %s  log2=%.3f  spread=%.3f",
                             gene.ljust(gene_cols), label.ljust(chrom_cols),
                             probe.log2, probe.spread)

    # Count the number of BG probes dropped, too (names are all "Background")
    bg_bad_probes = bad_probes[~fg_index]
    if len(bg_bad_probes) > 0:
        bad_pct = 100 * len(bg_bad_probes) / sum(probes['gene'] == 'Background')
        logging.info("Antitargets: %d (%s) bins failed filters",
                     len(bg_bad_probes), "%.4f" % bad_pct + '%')


def get_fasta_stats(probes, fa_fname):
    """Calculate GC and RepeatMasker content of each bin in the FASTA genome."""
    fa_coords = list(zip(probes.chromosome, probes.start, probes.end))
    logging.info("Calculating GC and RepeatMasker content in %s ...", fa_fname)
    gc_rm_vals = [calculate_gc_lo(subseq)
                  for subseq in ngfrills.fasta_extract_regions(fa_fname,
                                                               fa_coords)]
    gc_vals, rm_vals = list(zip(*gc_rm_vals))
    return np.asfarray(gc_vals), np.asfarray(rm_vals)


def calculate_gc_lo(subseq):
    """Calculate the GC and lowercase (RepeatMasked) content of a string."""
    cnt_at_lo = subseq.count('a') + subseq.count('t')
    cnt_at_up = subseq.count('A') + subseq.count('T')
    cnt_gc_lo = subseq.count('g') + subseq.count('c')
    cnt_gc_up = subseq.count('G') + subseq.count('C')
    tot = float(cnt_gc_up + cnt_gc_lo + cnt_at_up + cnt_at_lo)
    if not tot:
        return 0.0, 0.0
    frac_gc = (cnt_gc_lo + cnt_gc_up) / tot
    frac_lo = (cnt_at_lo + cnt_gc_lo) / tot
    return frac_gc, frac_lo


def reference2regions(reference, coord_only=False):
    """Extract iterables of target and antitarget regions from a reference.

    Like loading two BED files with ngfrills.parse_regions.
    """
    cna2rows = (_cna2coords if coord_only else _cna2regions)
    return list(map(cna2rows, _ref_split_targets(reference)))


def _cna2coords(cnarr):
    """Extract the coordinate columns from a CopyNumberArray"""
    return list(zip(cnarr['chromosome'], cnarr['start'], cnarr['end']))


def _cna2regions(cnarr):
    """Extract the region columns (including genes) from a CopyNumberArray"""
    return list(zip(cnarr['chromosome'], cnarr['start'], cnarr['end'], cnarr['gene']))


def _ref_split_targets(ref_arr):
    """Split reference into 2 sub-arrays of targets/antitargets."""
    is_bg = (ref_arr['gene'] == 'Background')
    targets = ref_arr[~is_bg]
    antitargets = ref_arr[is_bg]
    return targets, antitargets
