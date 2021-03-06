###############################################################################
#                                                                             #
#    This program is free software: you can redistribute it and/or modify     #
#    it under the terms of the GNU General Public License as published by     #
#    the Free Software Foundation, either version 3 of the License, or        #
#    (at your option) any later version.                                      #
#                                                                             #
#    This program is distributed in the hope that it will be useful,          #
#    but WITHOUT ANY WARRANTY; without even the implied warranty of           #
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the            #
#    GNU General Public License for more details.                             #
#                                                                             #
#    You should have received a copy of the GNU General Public License        #
#    along with this program. If not, see <http://www.gnu.org/licenses/>.     #
#                                                                             #
###############################################################################

import os
import sys
import logging
import psycopg2 as pg

from biolib.taxonomy import Taxonomy

from GenomeManager import GenomeManager
from GenomeListManager import GenomeListManager
from Exceptions import GenomeDatabaseError

from collections import Counter
from collections import defaultdict


class TreeManager(object):
    """Manages genomes, concatenated alignment, and metadata for tree inference and visualization."""

    def __init__(self, cur, currentUser):
        """Initialize.

        Parameters
        ----------
        cur : psycopg2.cursor
            Database cursor.
        currentUser : User
            Current user of database.
        """

        self.logger = logging.getLogger()

        self.cur = cur
        self.currentUser = currentUser
        
    def _taxa_filter(self, taxa_filter, genome_ids, guaranteed_ids, retain_guaranteed):
        """Filter genomes to specified taxa."""
        
        self.logger.info('Filtering genomes outside taxonomic groups of interest (%s).' % taxa_filter)
        taxa_to_retain = [x.strip() for x in taxa_filter.split(',')]
        genome_ids_from_taxa = self._genomesFromTaxa(genome_ids, taxa_to_retain)
        
        retained_guaranteed_ids = guaranteed_ids - genome_ids_from_taxa
        if retain_guaranteed:
            if len(retained_guaranteed_ids):
                self.logger.warning('Retaining %d guaranteed genomes from taxa not specified by the taxa filter.' % len(retained_guaranteed_ids))
                self.logger.warning("You can use the '--guaranteed_taxa_filter' flag to filter these genomes.")

            genomes_to_retain = genome_ids.intersection(genome_ids_from_taxa).union(guaranteed_ids)
        else:
            genomes_to_retain = genome_ids.intersection(genome_ids_from_taxa)
            self.logger.info("Filtered %d 'guaranteed' genomes based on taxonomic affiliations." % len(retained_guaranteed_ids))
            
        self.logger.info('Filtered %d genomes based on taxonomic affiliations.' % (
                                        len(genome_ids) - len(genomes_to_retain)))

        return genomes_to_retain
        
    def filterGenomes(self, marker_ids,
                      genome_ids,
                      quality_threshold,
                      quality_weight,
                      comp_threshold,
                      cont_threshold,
                      min_perc_aa,
                      min_rep_perc_aa,
                      taxa_filter,
                      guaranteed_taxa_filter,
                      genomes_to_exclude,
                      guaranteed_ids,
                      rep_ids,
                      directory,
                      prefix):
        """Filter genomes based on provided criteria.

        Parameters
        ----------

        Returns
        -------
        set
            Database identifiers of retained genomes.
        """

        if not os.path.exists(directory):
            os.makedirs(directory)

        # get mapping from db genome IDs to external IDs
        genome_mngr = GenomeManager(self.cur, self.currentUser)
        external_ids = genome_mngr.genomeIdsToExternalGenomeIds(genome_ids)
        filter_genome_file = os.path.join(directory, prefix + '_filtered_genomes.tsv')
        fout_filtered = open(filter_genome_file, 'w')

        self.logger.info('Filtering initial set of %d genomes.' % len(genome_ids))

        extra_guaranteed_ids = [x for x in guaranteed_ids if x not in genome_ids]
        if len(extra_guaranteed_ids) > 0:
            self.logger.warning('Identified {0} guaranteed genomes absent from specified input genomes (Those genomes will not appear in the final tree).'.format(len(extra_guaranteed_ids)))
            guaranteed_ids = [x for x in guaranteed_ids if x in genome_ids]
        self.logger.info('Identified %d genomes to be excluded from filtering.' % len(guaranteed_ids))

        # for all markers, get the expected marker size
        self.cur.execute("SELECT markers.id, markers.name, description, id_in_database, size, external_id_prefix " +
                         "FROM markers, marker_databases " +
                         "WHERE markers.id in %s "
                         "AND markers.marker_database_id = marker_databases.id "
                         "ORDER by external_id_prefix ASC, id_in_database ASC", (tuple(marker_ids),))

        chosen_markers = dict()
        chosen_markers_order = []

        total_alignment_len = 0
        for marker_id, marker_name, marker_description, id_in_database, size, external_id_prefix in self.cur:
            chosen_markers[marker_id] = {'external_id_prefix': external_id_prefix, 'name': marker_name,
                                         'description': marker_description, 'id_in_database': id_in_database, 'size': size}
            chosen_markers_order.append(marker_id)
            total_alignment_len += size

        # filter genomes based on taxonomy
        genomes_to_retain = genome_ids
        if taxa_filter:
            new_genomes_to_retain = self._taxa_filter(taxa_filter, 
                                                        genomes_to_retain, 
                                                        guaranteed_ids, 
                                                        retain_guaranteed=True)
            for genome_id in genomes_to_retain - new_genomes_to_retain:
                rep_str = 'Representative' if genome_id in rep_ids else ''
                fout_filtered.write('%s\t%s\t%s\n' % (external_ids[genome_id], 'Filtered on taxonomic affiliation.', rep_str))
                
            genomes_to_retain = new_genomes_to_retain

        if guaranteed_taxa_filter:
            new_genomes_to_retain = self._taxa_filter(guaranteed_taxa_filter, 
                                                        genomes_to_retain, 
                                                        guaranteed_ids, 
                                                        retain_guaranteed=False)
            for genome_id in genomes_to_retain - new_genomes_to_retain:
                rep_str = 'Representative' if genome_id in rep_ids else ''
                fout_filtered.write('%s\t%s\t%s\n' % (external_ids[genome_id], 'Filtered on guaranteed taxonomic affiliation.', rep_str))
                
            genomes_to_retain = new_genomes_to_retain

        # find genomes based on completeness, contamination, or genome quality
        self.logger.info('Filtering genomes with completeness <%.1f%%, contamination >%.1f%%, or quality <%.1f%% (weight = %.1f).' % (
            comp_threshold,
            cont_threshold,
            quality_threshold,
            quality_weight))
        filtered_genomes = self._filterOnGenomeQuality(genomes_to_retain,
                                                       quality_threshold,
                                                       quality_weight,
                                                       comp_threshold,
                                                       cont_threshold)

        # sanity check representatives are not of poor quality
        final_filtered_genomes = set()
        for genome_id, quality in filtered_genomes.iteritems():
            if genome_id not in guaranteed_ids:
                if genome_id in rep_ids:
                    self.logger.warning('Retaining representative genome %s despite poor estimated quality (comp=%.1f%%, cont=%.1f%%).' % (external_ids[genome_id], quality[0], quality[1]))
                else:
                    final_filtered_genomes.add(genome_id)
                    fout_filtered.write(
                        '%s\t%s\t%.2f\t%.2f\n' % (external_ids[genome_id],
                                                  'Filtered on quality (completeness, contamination).',
                                                  quality[0],
                                                  quality[1]))

        self.logger.info('Filtered %d genomes based on completeness, contamination, and quality.' % len(final_filtered_genomes))

        genomes_to_retain -= final_filtered_genomes

        # filter genomes explicitly specified for exclusion
        if genomes_to_exclude:
            for genome_id in genomes_to_exclude:
                if genome_id in external_ids:
                    fout_filtered.write('%s\t%s\n' % (external_ids[genome_id], 'Explicitly marked for exclusion.'))

            conflicting_genomes = guaranteed_ids.intersection(genomes_to_exclude)
            if conflicting_genomes:
                raise GenomeDatabaseError('Genomes marked for both retention and exclusion, e.g.: %s'
                                          % conflicting_genomes.pop())

            new_genomes_to_retain = genomes_to_retain.difference(genomes_to_exclude)
            self.logger.info('Filtered %d genomes explicitly indicated for exclusion.' % (
                len(genomes_to_retain) - len(new_genomes_to_retain)))
            genomes_to_retain = new_genomes_to_retain

        # filter genomes with insufficient number of amino acids in MSA
        self.logger.info('Filtering genomes with insufficient amino acids in the MSA.')
        filter_on_aa = set()
        for genome_id in genomes_to_retain:
            aligned_marker_query = ("SELECT sequence, multiple_hits " +
                                    "FROM aligned_markers " +
                                    "WHERE genome_id = %s " +
                                    "AND sequence is NOT NULL " +
                                    "AND marker_id IN %s")

            self.cur.execute(aligned_marker_query,
                             (genome_id, tuple(marker_ids)))

            total_aa = 0
            for sequence, multiple_hits in self.cur:
                if not multiple_hits:
                    total_aa += len(sequence) - sequence.count('-')

            # should retain guaranteed genomes unless they have zero amino acids in MSA
            if genome_id in guaranteed_ids:
                if total_aa != 0:
                    continue
                else:
                    self.logger.warning('Filtered guaranteed genome %s with zero amino acids in MSA.' % external_ids[genome_id])

            perc_alignment = total_aa * 100.0 / total_alignment_len
            if perc_alignment < min_perc_aa:
                rep_str = ''
                if genome_id in rep_ids:
                    if perc_alignment < min_rep_perc_aa:
                        rep_str = 'Representative'
                        self.logger.warning('Filtered representative genome %s due to lack of aligned amino acids (%.1f%%).' % (external_ids[genome_id], perc_alignment))
                    else:
                        self.logger.warning('Retaining representative genome %s despite small numbers of aligned amino acids (%.1f%%).' % (external_ids[genome_id], perc_alignment))
                        continue

                filter_on_aa.add(genome_id)
                fout_filtered.write(
                    '%s\t%s\t%d\t%.1f\t%s\n' % (external_ids[genome_id],
                                                'Insufficient number of amino acids in MSA (total AA, % alignment length)',
                                                total_aa,
                                                perc_alignment,
                                                rep_str))

        fout_filtered.close()

        self.logger.info('Filtered %d genomes with insufficient amino acids in the MSA.' % len(filter_on_aa))

        genomes_to_retain.difference_update(filter_on_aa)
        self.logger.info('Producing tree data for %d genomes.' % len(genomes_to_retain))

        good_genomes_file = os.path.join(
            directory, prefix + '_good_genomes.tsv')
        good_genomes = open(good_genomes_file, 'w')
        for item in genomes_to_retain:
            good_genomes.write("{0}\n".format(item))
        good_genomes.close()

        return (genomes_to_retain, chosen_markers_order, chosen_markers)

    def _mimagQualityInfo(self, metadata, col_headers):
        """Add MIMAG quality information to metadata."""
        
        col_headers.append('mimag_high_quality')
        col_headers.append('mimag_medium_quality')
        col_headers.append('mimag_low_quality')
        
        comp_index = col_headers.index('checkm_completeness')
        cont_index = col_headers.index('checkm_contamination')
        
        gtdb_domain_index = col_headers.index('gtdb_domain')
        lsu_5s_length_index = col_headers.index('lsu_5s_length')
        lsu_23s_length_index = col_headers.index('lsu_silva_length')
        ssu_length_index = col_headers.index('ssu_silva_length')
        trna_aa_count_index = col_headers.index('trna_aa_count')

        for i, gm in enumerate(metadata):
            comp = float(gm[comp_index])
            cont = float(gm[cont_index])
            
            lsu_5s_length = 0
            if gm[lsu_5s_length_index]:
                lsu_5s_length = int(gm[lsu_5s_length_index])
                
            lsu_23s_length = 0
            if gm[lsu_23s_length_index]:
                lsu_23s_length = int(gm[lsu_23s_length_index])
            
            ssu_length = 0
            if gm[ssu_length_index]:
                ssu_length = int(gm[ssu_length_index])
                
            trna_aa_count = 0
            if gm[trna_aa_count_index]:
                trna_aa_count = int(gm[trna_aa_count_index])
                
            gtdb_domain = gm[gtdb_domain_index]
            ssu_length_threshold = 1200
            if gtdb_domain == 'd__Archaea':
                ssu_length_threshold = 900
            
            hq = False
            mq = False
            lq = False
            if comp > 90 and cont < 5:
                if  (ssu_length >= ssu_length_threshold 
                        and lsu_23s_length >= 1900 
                        and lsu_5s_length >= 80
                        and trna_aa_count_index >= 18):
                    hq = True
                else:
                    mq = True
            elif comp >= 50 and cont < 10:
                mq = True
            elif cont < 10:
                lq = True
                
            gm += (hq, mq, lq)
            metadata[i] = gm
            
        return metadata
        
    def writeFiles(self,
                   marker_ids,
                   genomes_to_retain,
                   min_perc_taxa,
                   consensus,
                   min_perc_aa,
                   chosen_markers_order,
                   chosen_markers,
                   alignment,
                   individual,
                   directory,
                   prefix):
        '''
        Write summary files and arb files

        :param marker_ids:
        :param genomes_to_retain:
        :param chosen_markers_order:
        :param chosen_markers:
        :param alignment:
        :param individual:
        :param directory:
        :param prefix:
        '''

        if not os.path.exists(directory):
            os.makedirs(directory)

        # output the marker info and multiple hit info
        multi_hits_fh = open(
            os.path.join(directory, prefix + "_multi_hits.tsv"), 'wb')
        multi_hits_header = ["Genome_ID"]
        for marker_id in chosen_markers_order:
            external_id = chosen_markers[marker_id][
                'external_id_prefix'] + "_" + chosen_markers[marker_id]['id_in_database']
            multi_hits_header.append(external_id)
        multi_hits_fh.write("\t".join(multi_hits_header) + "\n")

        # select genomes to retain
        self.cur.execute("SELECT * " +
                         "FROM metadata_view "
                         "WHERE id IN %s", (tuple(genomes_to_retain),))
        col_headers = [desc[0] for desc in self.cur.description]
        metadata = self.cur.fetchall()
        
        # add MIMAG quality information
        #metadata = self._mimagQualityInfo(metadata, col_headers)

        # identify columns of interest
        genome_id_index = col_headers.index('id')
        genome_name_index = col_headers.index('accession')
        col_headers.remove('id')
        col_headers.remove('accession')

        # create ARB import filter
        arb_import_filter = os.path.join(directory, prefix + "_arb_filter.ift")
        self._arbImportFilter(col_headers, arb_import_filter)

        # run through each of the genomes and concatenate markers
        self.logger.info(
            'Concatenated marker genes for %d genomes.' % len(genomes_to_retain))

        individual_marker_fasta = dict()
        single_copy = defaultdict(int)
        ubiquitous = defaultdict(int)
        multi_hits_details = defaultdict(list)
        msa = {}
        for genome_metadata in metadata:
            # genome_metadata = list(genome_metadata)
            db_genome_id = genome_metadata[genome_id_index]
            external_genome_id = genome_metadata[genome_name_index]

            # get aligned markers
            aligned_marker_query = ("SELECT am.marker_id, sequence, multiple_hits, evalue " +
                                    "FROM aligned_markers am " +
                                    "LEFT JOIN markers m on m.id=am.marker_id " +
                                    "WHERE genome_id = %s " +
                                    "AND sequence is NOT NULL " +
                                    "AND marker_id in %s " +
                                    "ORDER BY m.id_in_database")

            self.cur.execute(aligned_marker_query,
                             (db_genome_id, tuple(marker_ids)))

            if (self.cur.rowcount == 0):
                self.logger.warning(
                    "Genome %s has no markers for this marker set and will be missing from the output files." % external_genome_id)
                continue

            genome_info = dict()
            genome_info['markers'] = dict()
            genome_info['multiple_hits'] = dict()
            for marker_id, sequence, multiple_hits, evalue in self.cur:
                if evalue:  # markers without an e-value are missing
                    genome_info['markers'][marker_id] = sequence
                genome_info['multiple_hits'][marker_id] = multiple_hits

            aligned_seq = ''
            for marker_id in chosen_markers_order:
                multiple_hits = genome_info['multiple_hits'][marker_id]

                if (marker_id in genome_info['markers']):
                    ubiquitous[marker_id] += 1
                    if not multiple_hits:
                        single_copy[marker_id] += 1
                        multi_hits_details[db_genome_id].append('Single')
                    else:
                        multi_hits_details[db_genome_id].append('Multiple')
                else:
                    multi_hits_details[db_genome_id].append('Missing')

                if (marker_id in genome_info['markers']) and not multiple_hits:
                    sequence = genome_info['markers'][marker_id]
                    fasta_outstr = ">%s\n%s\n" % (external_genome_id, sequence)

                    try:
                        individual_marker_fasta[marker_id].append(fasta_outstr)
                    except KeyError:
                        individual_marker_fasta[marker_id] = [fasta_outstr]
                else:
                    sequence = chosen_markers[marker_id]['size'] * '-'
                    fasta_outstr = ">%s\n%s\n" % (external_genome_id, sequence)

                    try:
                        individual_marker_fasta[marker_id].append(fasta_outstr)
                    except KeyError:
                        individual_marker_fasta[marker_id] = [fasta_outstr]
                aligned_seq += sequence

            msa[external_genome_id] = aligned_seq
            multi_hits_outstr = '%s\t%s\n' % (
                external_genome_id, '\t'.join(multi_hits_details[db_genome_id]))
            multi_hits_fh.write(multi_hits_outstr)

        multi_hits_fh.close()

        # filter columns without sufficient representation across taxa
        self.logger.info('Trimming columns with insufficient taxa or poor consensus.')
        trimmed_seqs, pruned_seqs, count_wrong_pa, count_wrong_cons, mask = self._trim_seqs(
            msa, min_perc_taxa / 100.0, consensus / 100.0, min_perc_aa / 100.0)
        self.logger.info('Trimmed alignment from %d to %d AA (%d by minimum taxa percent, %d by consensus).' % (len(msa[msa.keys()[0]]),
                                                                                                                len(trimmed_seqs[trimmed_seqs.keys()[0]]), count_wrong_pa, count_wrong_cons))
        self.logger.info('After trimming %d taxa have amino acids in <%.1f%% of columns.' % (
            len(pruned_seqs), min_perc_aa))

        # write out mask for MSA
        msa_mask_out = open(os.path.join(directory, prefix + "_mask.txt"), 'w')
        msa_mask_out.write(''.join(['1' if m else '0' for m in mask]))
        msa_mask_out.close()

        # write out MSA
        fasta_concat_filename = os.path.join(
            directory, prefix + "_concatenated.faa")
        fasta_concat_fh = open(fasta_concat_filename, 'wb')
        trimmed_seqs.update(pruned_seqs)
        for genome_id, aligned_seq in trimmed_seqs.iteritems():
            fasta_outstr = ">%s\n%s\n" % (genome_id, aligned_seq)
            fasta_concat_fh.write(fasta_outstr)
        fasta_concat_fh.close()

        # write out ARB metadata
        self.logger.info(
            'Writing ARB metadata for %d genomes.' % len(genomes_to_retain))
        arb_metadata_file = os.path.join(
            directory, prefix + "_arb_metadata.txt")
        arb_metadata_fh = open(arb_metadata_file, 'wb')

        for genome_metadata in metadata:
            # take special care of the genome identifier and name as these
            # are handle as a special case in the ARB metadata file
            genome_metadata = list(genome_metadata)
            db_genome_id = genome_metadata[genome_id_index]
            external_genome_id = genome_metadata[genome_name_index]
            del genome_metadata[max(genome_id_index, genome_name_index)]
            del genome_metadata[min(genome_id_index, genome_name_index)]

            # write out ARB record
            multiple_hit_count = sum(
                [1 if x == "Multiple" else 0 for x in multi_hits_details[db_genome_id]])
            msa_gene_count = sum(
                [1 if x == "Single" else 0 for x in multi_hits_details[db_genome_id]])

            aligned_seq = trimmed_seqs[external_genome_id]
            if not alignment:
                aligned_seq = ''
            self._arbRecord(arb_metadata_fh,
                            external_genome_id,
                            col_headers,
                            genome_metadata,
                            multiple_hit_count,
                            msa_gene_count,
                            len(chosen_markers_order),
                            aligned_seq)

        arb_metadata_fh.close()

        # write out marker gene summary info
        marker_info_fh = open(
            os.path.join(directory, prefix + "_markers_info.tsv"), 'wb')
        marker_info_fh.write(
            'Marker Id\tName\tDescription\tLength (bp)\tSingle copy (%)\tUbiquity (%)\n')

        for marker_id in chosen_markers_order:
            external_id = chosen_markers[marker_id][
                'external_id_prefix'] + "_" + chosen_markers[marker_id]['id_in_database']

            sc = single_copy.get(marker_id, 0) * 100.0 / len(genomes_to_retain)
            u = ubiquitous.get(marker_id, 0) * 100.0 / len(genomes_to_retain)

            out_str = "\t".join([
                external_id,
                chosen_markers[marker_id]['name'],
                chosen_markers[marker_id]['description'],
                str(chosen_markers[marker_id]['size']),
                '%.2f' % sc,
                '%.2f' % u
            ]) + "\n"
            marker_info_fh.write(out_str)
        marker_info_fh.close()

        # write out individual marker gene alignments
        if individual:
            self.logger.info('Writing individual alignments.')
            for marker_id in chosen_markers.keys():
                fasta_individual_fh = open(os.path.join(
                    directory, prefix + "_" + chosen_markers[marker_id]['id_in_database'] + ".faa"), 'wb')
                fasta_individual_fh.write(
                    ''.join(individual_marker_fasta[marker_id]))
                fasta_individual_fh.close()

        return fasta_concat_filename

    def _trim_seqs(self, seqs, min_per_taxa, consensus, min_per_bp):
        """Trim multiple sequence alignment.

        Adapted from the biolib package.

        Parameters
        ----------
        seqs : d[seq_id] -> sequence
            Aligned sequences.
        min_per_taxa : float
            Minimum percentage of taxa required to retain a column [0,1].
        min_per_bp : float
            Minimum percentage of base pairs required to keep trimmed sequence [0,1].
        Returns
        -------
        dict : d[seq_id] -> sequence
            Dictionary of trimmed sequences.
        dict : d[seq_id] -> sequence
            Dictionary of pruned sequences.
        """

        alignment_length = len(seqs.values()[0])

        # count number of taxa represented in each column
        column_count = [0] * alignment_length
        column_chars = [list() for _ in xrange(alignment_length)]
        for seq in seqs.values():
            for i, ch in enumerate(seq):
                if ch != '.' and ch != '-':
                    column_count[i] += 1
                    column_chars[i].append(ch)

        mask = [False] * alignment_length
        count_wrong_pa = 0
        count_wrong_cons = 0
        for i, count in enumerate(column_count):
            if count >= min_per_taxa * len(seqs):
                c = Counter(column_chars[i])
                if len(c.most_common(1)) == 0:
                    ratio = 0
                else:
                    _letter, count = c.most_common(1)[0]
                    ratio = float(count) / len(column_chars[i])
                if ratio >= consensus:
                    mask[i] = True
                else:
                    count_wrong_cons += 1
            else:
                count_wrong_pa += 1

        # trim columns
        output_seqs = {}
        pruned_seqs = {}
        for seq_id, seq in seqs.iteritems():
            masked_seq = ''.join([seq[i] for i in xrange(0, len(mask)) if mask[i]])

            valid_bases = len(masked_seq) - masked_seq.count('.') - masked_seq.count('-')
            if valid_bases < len(masked_seq) * min_per_bp:
                pruned_seqs[seq_id] = masked_seq
                continue

            output_seqs[seq_id] = masked_seq

        return output_seqs, pruned_seqs, count_wrong_pa, count_wrong_cons, mask

    def _filterOnGenomeQuality(self, genome_ids, quality_threshold, quality_weight, comp_threshold, cont_threshold):
        """Filter genomes on completeness and contamination thresholds.

        Parameters
        ----------
        genome_ids : list
            Database identifier for genomes of interest.
        quality_threshold : float
            Minimum required quality threshold.
        quality_weight : float
            Weighting factor for assessing genome quality.
        comp_threshold : float
            Minimum required completeness.
        cont_threshold : float
            Maximum permitted contamination.

        Returns
        -------
        set
            Database identifier of genomes failing quality filtering.
        """

        self.cur.execute("SELECT id, checkm_completeness, checkm_contamination " +
                         "FROM metadata_genes " +
                         "WHERE id IN %s " +
                         "AND (checkm_completeness < %s " +
                         "OR checkm_contamination > %s " +
                         "OR (checkm_completeness - %s*checkm_contamination) < %s)",
                         (tuple(genome_ids), comp_threshold, cont_threshold, quality_weight, quality_threshold))

        return {x[0]: [x[1], x[2]] for x in self.cur}

    def _genomesFromTaxa(self, genome_ids, taxa_to_retain):
        """Filter genomes to those within specified taxonomic groups.

        Parameters
        ----------
        genome_ids : list
            Database identifier for genomes of interest.
        taxa_to_retain : list
            Taxonomic groups of interest.

        Returns
        -------
        set
            Database identifier of genomes from specified taxonomic groups.
        """

        taxa_to_retain_at_rank = [[]
                                  for _ in xrange(len(Taxonomy.rank_prefixes))]
        for taxon in taxa_to_retain:
            taxon_prefix = taxon[0:3]
            if taxon_prefix not in Taxonomy.rank_prefixes:
                self.logger.error('Invalid taxon prefix: %s' % taxon)
                sys.exit(0)

            rank_index = Taxonomy.rank_index[taxon_prefix]
            taxa_to_retain_at_rank[rank_index].append(taxon)

        query_str = []
        query_tuple = [tuple(genome_ids)]
        for i, taxa in enumerate(taxa_to_retain_at_rank):
            if len(taxa):
                query_str.append('gtdb_%s IN %%s' % Taxonomy.rank_labels[i])
                query_tuple.append(tuple(taxa))
        query_str = ' OR '.join(query_str)

        self.cur.execute("SELECT id " +
                         "FROM metadata_taxonomy " +
                         "WHERE id IN %s "
                         "AND " + query_str,
                         tuple(query_tuple))

        genome_ids_from_taxa = set([x[0] for x in self.cur])

        return genome_ids_from_taxa

    def _arbImportFilter(self, metadata_fields, output_file):
        """Create ARB import filter.

        Parameters
        ----------
        metadata_fields : list
            Names of fields to import.
        output_file : str
            Name of output file.
        """
        fout = open(output_file, 'w')
        fout.write('AUTODETECT\t"BEGIN"\n\n')
        fout.write('BEGIN\t"BEGIN*"\n\n')

        fout.write('MATCH\t"%s\\=*"\n' % 'db_name')
        fout.write('\tSRT "*\\=="\n')
        fout.write('\tWRITE "%s"\n\n' % 'name')

        # place organism name near top for convenience
        fout.write('MATCH\t"%s\\=*"\n' % 'organism_name')
        fout.write('\tSRT "*\\=="\n')
        fout.write('\tWRITE "%s"\n\n' % 'organism_name')

        fields = metadata_fields + ['msa_gene_count',
                                    'msa_num_marker_genes',
                                    'msa_aa_count',
                                    'msa_length',
                                    'multiple_homologs']
        for field in fields:
            if field != 'organism_name':
                fout.write('MATCH\t"%s\\=*"\n' % field)
                fout.write('\tSRT "*\\=="\n')
                fout.write('\tWRITE "%s"\n\n' % field)

        fout.write('SEQUENCEAFTER\t"multiple_homologs*"\n')
        fout.write('SEQUENCESRT\t"*\\=="\n')
        fout.write('SEQUENCEEND\t"END"\n\n')
        fout.write('END\t"END"\n')

        fout.close()

    def _arbRecord(self, fout,
                   external_genome_id,
                   metadata_fields,
                   metadata_values,
                   multiple_hit_count,
                   msa_gene_count,
                   num_marker_genes,
                   aligned_seq):
        """Write out ARB record for genome."""

        # customize output relative to raw database table
        if external_genome_id.startswith('GB') or external_genome_id.startswith('RS'):
            metadata_values = list(metadata_values)
            organism_name_index = metadata_fields.index('organism_name')
            ncbi_organism_name_index = metadata_fields.index('ncbi_organism_name')
            metadata_values[organism_name_index] = metadata_values[ncbi_organism_name_index]

        fout.write("BEGIN\n")
        fout.write("db_name=%s\n" % external_genome_id)
        for col_header, value in zip(metadata_fields, metadata_values):
            if isinstance(value, float):
                value = '%.4g' % value

            # replace equal signs as these are incompatible with the ARB parser
            value = str(value)
            if value:
                value = value.replace('=', '/')

            fout.write("%s=%s\n" % (col_header, value))

        fout.write("msa_gene_count=%d\n" % msa_gene_count)
        fout.write("msa_num_marker_genes=%d\n" % num_marker_genes)
        fout.write("msa_aa_count=%d\n" % (len(aligned_seq) - aligned_seq.count('-')))
        fout.write("msa_length=%d\n" % len(aligned_seq))
        fout.write("multiple_homologs=%d\n" % multiple_hit_count)
        fout.write("aligned_seq=%s\n" % (aligned_seq))
        fout.write("END\n\n")
