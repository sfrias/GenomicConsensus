# Author: David Alexander, Jim Drake
from __future__ import absolute_import, division, print_function

import cProfile, logging, os.path, sys
from multiprocessing import Process
from threading import Thread
from collections import OrderedDict, defaultdict
from .options import options
from GenomicConsensus import reference, consensus, utils, windows
from .io.VariantsGffWriter import VariantsGffWriter
from .io.VariantsVcfWriter import VariantsVcfWriter
from pbcore.io import FastaWriter, FastqWriter

class ResultCollector(object):
    """
    Gathers results and writes to a file.
    """
    def __init__(self, resultsQueue, algorithmName, algorithmConfig):
        self._resultsQueue = resultsQueue
        self._algorithmName = algorithmName
        self._algorithmConfig = algorithmConfig

    def _run(self):
        self.onStart()

        sentinelsReceived = 0
        while sentinelsReceived < options.numWorkers:
            result = self._resultsQueue.get()
            if result is None:
                sentinelsReceived += 1
            else:
                self.onResult(result)

        self.onFinish()

    def run(self):
        if options.doProfiling:
            cProfile.runctx("self._run()",
                            globals=globals(),
                            locals=locals(),
                            filename=os.path.join(options.temporaryDirectory,
                                                  "profile-%s.out" % (self.name)))
        else:
            self._run()


    # ==================================
    # Overridable interface begins here.
    #

    def onStart(self):
        self.referenceBasesProcessedById = OrderedDict()
        for refId in reference.byName:
            self.referenceBasesProcessedById[refId] = 0
        self.variantsByRefId             = defaultdict(list)
        self.consensusChunksByRefId      = defaultdict(list)

        # open file writers
        self.fastaWriter = None
        self.fastqWriter = None
        self.gffWriter   = None
        self.vcfWriter   = None
        if options.fastaOutputFilename:
            self.fastaWriter = FastaWriter(options.fastaOutputFilename)
        if options.fastqOutputFilename:
            self.fastqWriter = FastqWriter(options.fastqOutputFilename)
        if options.gffOutputFilename:
            self.gffWriter = VariantsGffWriter(options.gffOutputFilename,
                                               vars(options),
                                               reference.byName.values())
        if options.vcfOutputFilename:
            self.vcfWriter = VariantsVcfWriter(options.vcfOutputFilename,
                                               vars(options),
                                               reference.byName.values())

    def onResult(self, result):
        window, cssAndVariants = result
        css, variants = cssAndVariants
        self._recordNewResults(window, css, variants)
        self._flushContigIfCompleted(window)

    def onFinish(self):
        logging.info("Analysis completed.")
        if self.fastaWriter: self.fastaWriter.close()
        if self.fastqWriter: self.fastqWriter.close()
        if self.gffWriter:   self.gffWriter.close()
        if self.vcfWriter:   self.vcfWriter.close()
        logging.info("Output files completed.")

    def _recordNewResults(self, window, css, variants):
        refId, refStart, refEnd = window
        self.consensusChunksByRefId[refId].append(css)
        self.variantsByRefId[refId] += variants
        self.referenceBasesProcessedById[refId] += (refEnd - refStart)

    def _flushContigIfCompleted(self, window):
        refId, _, _ = window
        refEntry = reference.byName[refId]
        refName = refEntry.fullName
        basesProcessed = self.referenceBasesProcessedById[refId]
        requiredBases = reference.numReferenceBases(refId, options.referenceWindows)
        if basesProcessed == requiredBases:
            # This contig is done, so we can dump to file and delete
            # the data structures.
            if self.gffWriter or self.vcfWriter:
                variants = sorted(self.variantsByRefId[refId])
                if self.gffWriter:
                    self.gffWriter.writeVariants(variants)
                if self.vcfWriter:
                    self.vcfWriter.writeVariants(variants)
            del self.variantsByRefId[refId]

            #
            # If the user asked to analyze a window or a set of
            # windows, we output a FAST[AQ] contig per analyzed
            # window.  Otherwise we output a fasta contig per
            # reference contig.
            #
            # We try to be intelligent about naming the output
            # contigs, to include window information where applicable.
            #
            for span in reference.enumerateSpans(refId, options.referenceWindows):
                _, s, e = span
                if (s == 0) and (e == refEntry.length):
                    spanName = refName
                else:
                    spanName = refName + "_%d_%d" % (s, e)
                cssName = consensus.consensusContigName(spanName, self._algorithmName)
                # Gather just the chunks pertaining to this span
                chunksThisSpan = [ chunk for chunk in self.consensusChunksByRefId[refId]
                                   if windows.windowsIntersect(chunk.refWindow, span) ]
                css = consensus.join(chunksThisSpan)

                if self.fastaWriter:
                    self.fastaWriter.writeRecord(cssName,
                                                 css.sequence)
                if self.fastqWriter:
                    self.fastqWriter.writeRecord(cssName,
                                                 css.sequence,
                                                 css.confidence)

            del self.consensusChunksByRefId[refId]

class ResultCollectorProcess(ResultCollector, Process):
    def __init__(self, *args):
        Process.__init__(self)
        self.daemon = True
        super(ResultCollectorProcess,self).__init__(*args)

class ResultCollectorThread(ResultCollector, Thread):
    def __init__(self, *args):
        Thread.__init__(self)
        self.daemon = True
        self.exitcode = 0
        super(ResultCollectorThread,self).__init__(*args)
