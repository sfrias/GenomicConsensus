# Author: Lance Hepler
from __future__ import absolute_import, division, print_function

import time
from textwrap import dedent
from GenomicConsensus import __VERSION__, reference

def vcfVariantFrequency(var, labels):
    if var.frequency1 is None:
        return None
    elif var.isHeterozygous:
        denom = var.frequency1 + var.frequency2
        names = ['frequency{}'.format(label) for label in labels]
        freqs = [getattr(var, name) for name in names if getattr(var, name) is not None]
        return 'AF={}'.format(','.join('{:.3g}'.format(f / denom) for f in freqs))
    else:
        # the frequency is 100%, so no need
        return None

class VariantsVcfWriter(object):

    def __init__(self, f, optionsDict, referenceEntries):
        self._vcfFile = open(f, "w")
        self._minConfidence = optionsDict["minConfidence"]
        self._minCoverage = optionsDict["minCoverage"]

        print(dedent('''\
            ##fileformat=VCFv4.2
            ##fileDate={date}
            ##source=GenomicConsensusV{version}
            ##reference={reference}''').format(
                date=time.strftime("%Y%m%d"),
                version=__VERSION__,
                reference="file://" + optionsDict["referenceFilename"],
                ), file=self._vcfFile)
        # reference contigs
        for entry in referenceEntries:
            print("##contig=<ID={name},length={length}>".format(
                name=entry.name,
                length=entry.length
                # TODO(lhepler): evaluate adding md5 hexdigest here on large genomes
                ), file=self._vcfFile)
        print('##INFO=<ID=DP,Number=1,Type=Integer,Description="Approximate read depth; some reads may have been filtered">',
              file=self._vcfFile)
        if optionsDict["diploid"]:
            print('##INFO=<ID=AF,Number=A,Type=Float,Description="Allele Frequency">',
                  file=self._vcfFile)

        # filters
        self._minConfidenceFilterID = 'q{}'.format(self._minConfidence)
        if self._minConfidence > 0:
            print('##FILTER=<ID={id},Description="Quality below {confidence}">'.format(
                id=self._minConfidenceFilterID,
                confidence=self._minConfidence
                ), file=self._vcfFile)
        self._minCoverageFilterID = 'c{}'.format(self._minCoverage)
        if self._minCoverage > 0:
            print('##FILTER=<ID={id},Description="Coverage below {coverage}">'.format(
                id=self._minCoverageFilterID,
                coverage=self._minCoverage
                ), file=self._vcfFile)

        print("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO", file=self._vcfFile)

    def writeVariants(self, variants):
        for var in variants:
            pos = var.refStart
            ref = ""
            alt = ""
            labels = (1, 2)
            # insertion or deletion
            if var.refSeq == "" or var.readSeq1 == "" or \
                    (var.isHeterozygous and var.readSeq2 == ""):
                # we're anchored on the previous base so no 0- to 1-indexing
                #   correction required
                ref = var.refPrev + var.refSeq
                if var.isHeterozygous:
                    alt = ",".join(var.readPrev + seq for seq in (var.readSeq1, var.readSeq2))
                else:
                    alt = var.readPrev + var.readSeq1
            # substitution
            else:
                # due to 1-indexing, pos needs to be incremented
                pos += 1
                ref = var.refSeq
                if var.isHeterozygous:
                    alt = ",".join(seq for seq in (var.readSeq1, var.readSeq2))
                    if var.refSeq == var.readSeq1:
                        # first variant is same as wildtype
                        alt = var.readSeq2
                        labels = (2,)
                    elif var.refSeq == var.readSeq2:
                        # second variant is same as wildtype
                        alt = var.readSeq1
                        labels = (1,)
                    else:
                        # both variants differ from wildtype
                        alt = ",".join(seq for seq in (var.readSeq1, var.readSeq2))
                else:
                    alt = var.readSeq1
            freq = vcfVariantFrequency(var=var, labels=labels)
            info = "DP={0}".format(var.coverage)
            if freq:
                info = info + ";" + freq

            # failed filters
            failedFilters = []
            if var.confidence < self._minConfidence:
                failedFilters.append(self._minConfidenceFilterID)
            if var.coverage < self._minCoverage:
                failedFilters.append(self._minCoverageFilterID)
            filterText = ";".join(failedFilters) if failedFilters else "PASS"

            print("{chrom}\t{pos}\t{id}\t{ref}\t{alt}\t{qual}\t{filter}\t{info}".format(
                chrom=reference.idToFullName(var.refId),
                pos=pos,
                id=".",
                ref=ref,
                alt=alt,
                qual=var.confidence,
                filter=filterText,
                info=info), file=self._vcfFile)

    def close(self):
        self._vcfFile.close()
