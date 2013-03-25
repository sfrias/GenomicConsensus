#################################################################################
# Copyright (c) 2011-2013, Pacific Biosciences of California, Inc.
#
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# * Redistributions of source code must retain the above copyright
#   notice, this list of conditions and the following disclaimer.
# * Redistributions in binary form must reproduce the above copyright
#   notice, this list of conditions and the following disclaimer in the
#   documentation and/or other materials provided with the distribution.
# * Neither the name of Pacific Biosciences nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# NO EXPRESS OR IMPLIED LICENSES TO ANY PARTY'S PATENT RIGHTS ARE GRANTED BY
# THIS LICENSE.  THIS SOFTWARE IS PROVIDED BY PACIFIC BIOSCIENCES AND ITS
# CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
# PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL PACIFIC BIOSCIENCES OR
# ITS CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR
# BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER
# IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#################################################################################

# Author: David Alexander

from __future__ import absolute_import


import collections, h5py, math, logging, numpy as np, os, pprint
import sys, time
from .. import reference
from ..options import options
from ..Worker import WorkerProcess, WorkerThread
from ..ResultCollector import ResultCollectorProcess, ResultCollectorThread
from pbcore.io import rangeQueries, GffWriter
from ..io.fastx import FastaWriter, FastqWriter
from ..io.VariantsGffWriter import VariantsGffWriter

import ConsensusCore as cc
from GenomicConsensus.utils import noEvidenceConsensusCall, die
from GenomicConsensus.consensus import *
from GenomicConsensus.quiver.utils import *
from GenomicConsensus.quiver.model import *


class QuiverConfig(object):
    def __init__(self,
                 minMapQV=10,
                 minPoaCoverage=3,
                 maxPoaCoverage=11,
                 mutationSeparation=10,
                 maxIterations=20,
                 refineDinucleotideRepeats=True,
                 noEvidenceConsensus="nocall",
                 computeConfidence=True,
                 readStumpinessThreshold=0.1,
                 parameters=None):

        self.minMapQV                   = minMapQV
        self.minPoaCoverage             = minPoaCoverage
        self.maxPoaCoverage             = maxPoaCoverage
        self.mutationSeparation         = mutationSeparation
        self.maxIterations              = maxIterations
        self.refineDinucleotideRepeats  = refineDinucleotideRepeats
        self.noEvidenceConsensus        = noEvidenceConsensus
        self.computeConfidence          = computeConfidence
        self.readStumpinessThreshold    = readStumpinessThreshold
        self.parameters                 = parameters

        # Convenience
        self.model                      = self.parameters.model
        self.ccQuiverConfig             = self.parameters.quiverConfig


#
# The type used in the wire protocol.  Note that whereas plurality
# passes a packet per locus, we pass a bigger packet per reference
# window (1000bp, typically).
#
QuiverWindowSummary = collections.namedtuple("QuiverWindowSummary",
                                             ("referenceId",
                                              "referenceStart",
                                              "referenceEnd",
                                              "consensus",      # string
                                              "qv",             # numpy array of uint8
                                              "variants"))      # list of Variant



def filterAlnsForQuiver(refWindow, alns, quiverConfig):
    """
    Given alns (already clipped to the window bounds), filter out any
    that are incompatible with Quiver.

    By and large we avoid doing any filtering to avoid potential
    reference bias in variant calling.

    However at the moment the POA (and potentially other components)
    breaks when there is a read of zero length.  So we throw away
    reads that are "stumpy", where the aligner has inserted a large
    gap, such that while the alignment technically spans the window,
    it may not have any read content therein:

          Ref   ATGATCCAGTTACTCCGATAAA
          Read  ATG---------------TA-A
          Win.     [              )
    """
    _, winStart, winEnd = refWindow
    winLen = winEnd - winStart
    minimumReadLength = quiverConfig.readStumpinessThreshold * winLen
    return [ aln for aln in alns
             if aln.readLength >= (quiverConfig.readStumpinessThreshold * aln.referenceSpan) ]

def quiverConsensusAndVariantsForWindow(cmpH5, refWindow, referenceContig,
                                        depthLimit, quiverConfig):
    """
    High-level routine for calling the consensus for a
    window of the genome given a cmp.h5.

    Identifies the coverage contours of the window in order to
    identify subintervals where a good consensus can be called.
    Creates the desired "no evidence consensus" where there is
    inadequate coverage.
    """
    winId, winStart, winEnd = refWindow
    refSequence = referenceContig[winStart:winEnd].tostring()
    logging.info("Quiver operating on window: %s" %
                 reference.windowToString(refWindow))

    noEvidenceConsensusFactory = \
        noEvidenceConsensusFactoryByName[quiverConfig.noEvidenceConsensus]

    # 1) identify the intervals with adequate coverage for quiver
    #    consensus; restrict to intervals of length > 10
    allRows = readsInWindow(cmpH5, refWindow, minMapQV=quiverConfig.minMapQV)
    starts = cmpH5.tStart[allRows]
    ends   = cmpH5.tEnd[allRows]
    intervals = [ (s, e)
                  for (s, e) in kSpannedIntervals(refWindow,
                                                  quiverConfig.minPoaCoverage,
                                                  starts,
                                                  ends)
                  if (e - s) > 10 ]

    coverageGaps = holes(refWindow, intervals)
    allIntervals = sorted(intervals + coverageGaps)
    if len(allIntervals) > 1:
        logging.info("Usable coverage in window %s in intervals: %r" %
                     (reference.windowToString(refWindow), intervals))

    # 2) pull out the reads we will use for each interval
    # 3) call quiverConsensusForAlignments on the interval
    subConsensi = []
    rowNumbersUsed = set()

    for interval in allIntervals:
        intStart, intEnd = interval
        intRefSeq = referenceContig[intStart:intEnd].tostring()
        subWin = subWindow(refWindow, interval)

        windowRefSeq = referenceContig[intStart:intEnd]
        rows = readsInWindow(cmpH5, subWin,
                             depthLimit=depthLimit,
                             minMapQV=quiverConfig.minMapQV,
                             strategy="longest")
        rowNumbersUsed.update(rows)

        alns = cmpH5[rows]
        clippedAlns_ = [ aln.clippedTo(*interval) for aln in alns ]
        clippedAlns = filterAlnsForQuiver(subWin, clippedAlns_, quiverConfig)

        if len([ aln for aln in clippedAlns
                 if aln.spansReferenceRange(*interval) ]) >= quiverConfig.minPoaCoverage:

            css = quiverConsensusForAlignments(subWin,
                                               intRefSeq,
                                               clippedAlns,
                                               quiverConfig)
        else:

            cssSeq = noEvidenceConsensusFactory(intRefSeq)
            css = Consensus(subWin,
                            cssSeq,
                            [0]*len(cssSeq))

        subConsensi.append(css)

    # 4) glue the subwindow consensus objects together to form the
    #    full window consensus
    cssSeq_  = "".join(sc.sequence for sc in subConsensi)
    cssConf_ = np.concatenate([sc.confidence for sc in subConsensi])
    css = Consensus(refWindow,
                    cssSeq_,
                    cssConf_)

    # 5) identify variants
    siteCoverage = rangeQueries.getCoverageInRange(cmpH5, refWindow, list(rowNumbersUsed))
    variants = variantsFromConsensus(refWindow, refSequence,
                                     cssSeq_, cssConf_, siteCoverage,
                                     options.aligner)

    # 6) Return
    return css, variants




class QuiverWorker(object):

    def onStart(self):
        # FIXME: move this out of onStart
        params = fetchParameterSet(self._inCmpH5,
                                   options.parametersFile,
                                   options.parameterSet)
        logging.info("Using parameter set %s" % params.name)

        # FIXME: hook up other options!
        self.quiverConfig = QuiverConfig(minMapQV=options.mapQvThreshold,
                                         noEvidenceConsensus=options.noEvidenceConsensusCall,
                                         parameters=params)


    def onChunk(self, referenceWindow):
        refId, refStart, refEnd = referenceWindow
        _, eStart, eEnd = eWindow = reference.enlargedReferenceWindow(referenceWindow, options.referenceChunkOverlap)

        # We call consensus on the enlarged window and then map back
        # to the reference and clip the consensus at the implied
        # bounds.  This seems to be more reliable thank cutting the
        # consensus bluntly
        refContig = reference.byId[refId].sequence
        refSequenceInEnlargedWindow = refContig[eStart:eEnd].tostring()

        #
        # Get the consensus for the enlarged window.
        #
        css_, variants_ = \
            quiverConsensusAndVariantsForWindow(self._inCmpH5, eWindow,
                                                refContig, options.coverage, self.quiverConfig)

        #
        # Restrict the consensus and variants to the reference window.
        #
        ga = cc.Align(refSequenceInEnlargedWindow, css_.sequence)
        targetPositions = cc.TargetToQueryPositions(ga)
        cssStart = targetPositions[refStart-eStart]
        cssEnd   = targetPositions[refEnd-eStart]

        cssSequence    = css_.sequence[cssStart:cssEnd]
        cssQv          = css_.confidence[cssStart:cssEnd]
        variants       = [ v for v in variants_
                           if refStart <= v.refStart < refEnd ]

        return QuiverWindowSummary(refId, refStart, refEnd,
                                   cssSequence, cssQv, variants)


ContigConsensusChunk = collections.namedtuple("ContigConsensusChunk",
                                              ("refStart", "refEnd", "consensus", "qv"))

class QuiverResultCollector(object):

    def onStart(self):
        self.allVariants = []
        # this is a map of refId -> [ContigConsensusChunk];
        self.consensusChunks = collections.defaultdict(list)

    def onResult(self, result):
        assert result.consensus != None and isinstance(result.consensus, str)
        self.allVariants += result.variants
        cssChunk = ContigConsensusChunk(result.referenceStart,
                                        result.referenceEnd,
                                        result.consensus,
                                        result.qv)
        self.consensusChunks[result.referenceId].append(cssChunk)

    def onFinish(self):
        # 1. GFF output.
        if options.gffOutputFilename:
            # Dictionary of refId -> [Variant]
            filteredVariantsByRefId = collections.defaultdict(list)
            for v in self.allVariants:
                if (v.confidence > options.variantConfidenceThreshold and
                    v.coverage   > options.variantCoverageThreshold):
                    filteredVariantsByRefId[v.refId].append(v)
            # Sort variants before output
            for refId in filteredVariantsByRefId:
                filteredVariantsByRefId[refId].sort()
            self.writeVariantsGff(options.gffOutputFilename, filteredVariantsByRefId)

        # 2. FASTA output.  Currently unoptimized--will choke hard on
        # very large references.
        if options.fastaOutputFilename:
            writer = FastaWriter(options.fastaOutputFilename)
            for refId, unsortedChunks in self.consensusChunks.iteritems():
                chunks = sorted(unsortedChunks)
                css = "".join(chunk.consensus for chunk in chunks)
                quiverHeader = reference.idToName(refId) + "|quiver"
                writer.writeRecord(quiverHeader, css)

        # 3. FASTQ output
        if options.fastqOutputFilename:
            writer = FastqWriter(options.fastqOutputFilename)
            for refId, unsortedChunks in self.consensusChunks.iteritems():
                chunks = sorted(unsortedChunks)
                css = "".join(chunk.consensus for chunk in chunks)
                qv = np.concatenate([chunk.qv for chunk in chunks])
                quiverHeader = reference.idToName(refId) + "|quiver"
                writer.writeRecord(quiverHeader, css, qv)


    def writeVariantsGff(self, filename, filteredVariantsByRefId):
        writer = VariantsGffWriter(options.gffOutputFilename,
                                   " ".join(sys.argv),
                                   reference.byId.values())
        for id in reference.byId:
            for v in filteredVariantsByRefId[id]:
                writer.writeRecord(v.toGffRecord())


#
# Slave process/thread classes
#
class QuiverWorkerProcess(QuiverWorker, WorkerProcess): pass
class QuiverWorkerThread(QuiverWorker, WorkerThread): pass
class QuiverResultCollectorProcess(QuiverResultCollector, ResultCollectorProcess): pass
class QuiverResultCollectorThread(QuiverResultCollector, ResultCollectorThread):  pass


#
# Plugin API
#
__all__ = [ "name",
            "availability",
            "additionalDefaultOptions",
            "compatibilityWithCmpH5",
            "slaveFactories" ]

name = "Quiver"
availability = (True, "OK")
additionalDefaultOptions = { "referenceChunkOverlap"      : 5,
                             "variantCoverageThreshold"   : 5,
                             "variantConfidenceThreshold" : 40,
                             "coverage"                   : 100,
                             "parameterSet"               : "best" }


def fetchParameterSet(cmpH5, parametersFileOrDirectory, parameterSetName):
    parametersFile = findParametersFile(parametersFileOrDirectory)
    logging.info("Using Quiver parameter sets from %s" % parametersFile)
    parameterSets = loadParameterSets(parametersFile)
    if options.parameterSet == "best":
        chemistry = majorityChemistry(cmpH5)
        params = bestParameterSet(parameterSets.values(),
                                  chemistry,
                                  cmpH5.pulseFeaturesAvailable())
    else:
        try:
            params = parameterSets[parameterSetName]
        except:
            die("Quiver: no available parameter set named %s" % parameterSetName)
    return params


def compatibilityWithCmpH5(cmpH5):
    if cmpH5.readType != "standard":
        return (False, "The Quiver algorithm requires a cmp.h5 file containing " + \
                       "standard (non-CCS) reads.")

    params = fetchParameterSet(cmpH5,
                               options.parametersFile,
                               options.parameterSet)

    if not params.model.isCompatibleWithCmpH5(cmpH5):
        return (False, "Selected Quiver parameter set is incompatible with this " + \
                        "cmp.h5 file due to missing data tracks.")

    if options.parameterSet == "best" and params.model != AllQVsModel:
        logging.warn(
            "This .cmp.h5 file lacks some of the QV data tracks that are required " +
            "for optimal performance of the Quiver algorithm.  For optimal results" +
            " use the ResequencingQVs workflow in SMRTPortal with bas.h5 files "    +
            "from an instrument using software version 1.3.1 or later.")

    return (True, "OK")

def slaveFactories(threaded):
    # By default we use slave processes. The tuple ordering is important.
    if threaded:
        return (QuiverWorkerThread,  QuiverResultCollectorThread)
    else:
        return (QuiverWorkerProcess, QuiverResultCollectorProcess)
