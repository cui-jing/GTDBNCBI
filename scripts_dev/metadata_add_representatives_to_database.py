#!/usr/bin/env python

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

__prog_name__ = 'metadata_add_representatives_to_database.py'
__prog_desc__ = 'Add representative genomes to database.'

__author__ = 'Donovan Parks'
__copyright__ = 'Copyright 2016'
__credits__ = ['Donovan Parks']
__license__ = 'GPL3'
__version__ = '0.0.1'
__maintainer__ = 'Donovan Parks'
__email__ = 'donovan.parks@gmail.com'
__status__ = 'Development'

import os
import sys
import csv
import logging
import argparse
import tempfile
from collections import defaultdict

from gtdb import GenomeDatabase
from gtdb.Exceptions import (GenomeDatabaseError, 
                                DumpDBErrors, 
                                DumpDBWarnings, 
                                ErrorReport)
                                
import psycopg2
from psycopg2.extensions import AsIs


class AddRepresentativeGenomes(object):
  """Populate 'gtdb_genome_representative' and 'gtdb_representative' fields in database."""

  def __init__(self):    
    logging.basicConfig(format="[%(asctime)s] %(levelname)s: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S",
                            level=logging.DEBUG)
    self.logger = logging
    
  def setup_db(self, gtdb_version):
    """Setup database."""

    # initialise the backend
    self.db = GenomeDatabase.GenomeDatabase(1, False)
    self.db.conn.MakePostgresConnection(gtdb_version)

    # login
    try:
        self.db.Login(None, False)
    except GenomeDatabaseError as e:
        self.db.conn.ClosePostgresConnection()
        ErrorReport(e.message + " The following error(s) were reported:\n")
        DumpDBErrors(self.db)
        sys.exit(-1)

  def run(self, cluster_file, gtdb_version):
    """Add metadata."""
    
    self.setup_db(gtdb_version)
    
    # clear representative fields
    cur = self.db.conn.cursor()
        
    q = ("UPDATE metadata_taxonomy SET gtdb_representative = NULL, gtdb_genome_representative = NULL")
    cur.execute(q)
    self.db.conn.commit()
    
    # mark all genomes as not being representatives
    q = ("SELECT accession FROM metadata_view")
    cur.execute(q)
    
    is_rep = {}
    for r in cur:
        is_rep[r[0]] = False
    
    # determine representative assignment of genomes
    temp_genome_rep_file = tempfile.NamedTemporaryFile(delete=False)
    for line in open(cluster_file):
        line_split = line.strip().split('\t')
        
        rep_genome = line_split[0]
        genome_ids = None
        if len(line_split) == 4:
            genome_ids = line_split[3].split(',')
            for genome_id in genome_ids:
                temp_genome_rep_file.write('%s\t%s\n' % (genome_id, rep_genome))
            
        temp_genome_rep_file.write('%s\t%s\n' % (rep_genome, rep_genome))
        is_rep[rep_genome] = True
    temp_genome_rep_file.close()
     
    cmd = 'gtdb -r metadata import --table metadata_taxonomy --field gtdb_genome_representative --type TEXT --metadatafile %s' % (temp_genome_rep_file.name)
    print cmd
    os.system(cmd)
    os.remove(temp_genome_rep_file.name)
    
    # mark representative genomes
    temp_rep_file = tempfile.NamedTemporaryFile(delete=False)
    for genome_id, rep_status in is_rep.iteritems():
        temp_rep_file.write('%s\t%s\n' % (genome_id, str(rep_status)))
    temp_rep_file.close()
    
    cmd = 'gtdb -r metadata import --table metadata_taxonomy --field gtdb_representative --type BOOLEAN --metadatafile %s' % (temp_rep_file.name)
    print cmd
    os.system(cmd)
    os.remove(temp_rep_file.name)  
    cur.close()

if __name__ == '__main__':
    print __prog_name__ + ' v' + __version__ + ': ' + __prog_desc__
    print '  by ' + __author__ + ' (' + __email__ + ')' + '\n'

    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('cluster_file', help="cluster file produced with 'genometreetk cluster'")
    parser.add_argument('gtdb_version', help='GTDB database version (i.e., gtdb_releaseX)')

    args = parser.parse_args()

    try:
        p = AddRepresentativeGenomes()
        p.run(args.cluster_file, args.gtdb_version)
    except SystemExit:
        print "\nControlled exit resulting from an unrecoverable error or warning."
    except:
        print "\nUnexpected error:", sys.exc_info()[0]
        raise
