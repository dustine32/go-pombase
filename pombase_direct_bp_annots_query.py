import argparse
import datetime
import logging
import math
import sys
import json

from ontobio.assoc_factory import AssociationSetFactory
from ontobio.ontol_factory import OntologyFactory
from gaf_annotation_set import GafAnnotationSet

# logging.basicConfig(level="DEBUG")

POMBASE = "NCBITaxon:4896"

def setup_pombase():
    ontology = OntologyFactory().create("go")
    afactory = AssociationSetFactory()
    association_set = afactory.create(ontology, "gene", "function", taxon=POMBASE)
    return ontology, association_set

# onto, aset = setup_pombase()

class GOTermAnalyzer():
    def __init__(self, onto):
        # TODO: Make this not dependent on prequeried annotations
        self.onto = onto

    def get_ancestors(self, go_term):
        ### BFO:0000050 = part of
        ### I assume "subClassOf" = is a?
        all_ancestors = self.onto.ancestors(go_term)
        all_ancestors.append(go_term)
        subont = self.onto.subontology(all_ancestors)
        return subont.ancestors(go_term, relations=["subClassOf","BFO:0000050"])

    def get_parents(self, go_term):
        all_parents = self.onto.parents(go_term)
        all_parents.append(go_term)
        subont = self.onto.subontology(all_parents)
        return subont.parents(go_term, relations=["subClassOf","BFO:0000050"])

    def is_biological_process(self, go_term):
        bp_root = "GO:0008150"
        if go_term == bp_root:
            return True
        ancestors = self.get_ancestors(go_term)
        if bp_root in ancestors:
            return True
        else:
            return False

    def is_molecular_function(self, go_term):
        mf_root = "GO:0003674"
        if go_term == mf_root:
            return True
        ancestors = self.get_ancestors(go_term)
        if mf_root in ancestors:
            return True
        else:
            return False

    def is_cellular_component(self, go_term):
        cc_root = "GO:0005575"
        if go_term == cc_root:
            return True
        ancestors = self.get_ancestors(go_term)
        if cc_root in ancestors:
            return True
        else:
            return False

    def get_ancestor_bps(self, mf_go_term):
        bp_ancestors = []
        # for ancestor in onto.ancestors(mf_go_term):
        for ancestor in self.get_ancestors(mf_go_term):
            if self.is_biological_process(ancestor):
                bp_ancestors.append(ancestor)
        return bp_ancestors

    def label(self, subject_id):
        return self.onto.label(subject_id)

class TermAnnotationDictionary():

    def __init__(self, ontology, annotation_set, json_file=None):
        self.bps = {}    ### Dictionary of PomBase gene ID's grouped by BP GO term
        self.ontology = ontology
        self.annotation_set = annotation_set
        self.candidate_bps = None
        self.pair_list = None
        self.cluster_list = None
        self.unpaired_bps = None
        self.analyzer = GOTermAnalyzer(ontology)
        self.grouper = BPTermSimilarityGrouper(self)
        if json_file is not None:
            with open(json_file, "r") as f:
                self.bps = json.loads(f.read())
                print("File '" + json_file + "' used to load gene-to-BP term dictionary - " + str(len(self.bps)) + " keys loaded")
        else:
            progress = ProgressTracker(len(annotation_set.association_map), "initializing gene-to-BP term dictionary")
            # progress = ProgressTracker(len(annotation_set), "initializing gene-to-BP term dictionary")
            ### subject_id = PomBase:SP######.## identifier - e.g. "PomBase:SPBP19A11.06"
            # for assoc in annotation_set:
            for subject_id in annotation_set.association_map:
                # subject_id = assoc["subject"]["id"]
                objects_for_subject = annotation_set.objects_for_subject(subject_id)
                for object_id in objects_for_subject:
                    if self.analyzer.is_biological_process(object_id):
                        ancestor_bps = self.analyzer.get_ancestor_bps(object_id)
                        ancestor_bps.append(object_id)
                        for bp in ancestor_bps:
                            if bp in self.bps:
                                if subject_id not in self.bps[bp]:
                                    self.bps[bp].append(subject_id)
                            else:
                                self.bps[bp] = [subject_id]
                progress.print_progress()

    def dump_to_json(self, filename):
        with open(filename, "w") as f:
            f.write(json.dumps(self.bps))

    def print_results(self, filepath=None, alt_bps=None):
        bps = self.bps
        if alt_bps is not None:
            bps = alt_bps
        if filepath is None:
            for key in sorted(bps, key=lambda key: len(bps[key]), reverse=False):
                print(str(len(bps[key])) + " - " + key + " - " + self.annotation_set.label(key))
            print("Total BPs: " + str(len(bps)))
        else:
            with open(filepath, 'w') as f:
                f.write("Total BPs: " + str(len(bps)) + "\n")
                for key in sorted(bps, key=lambda key: len(bps[key]), reverse=True):
                    f.write(str(len(bps[key])) + " - " + key + " - " + self.annotation_set.label(key) + "\n")

    def has_parent_with_direct_annotations_greater_than(self, go_term, n=30):
        for p in self.analyzer.get_parents(go_term):
            if p in self.bps and len(self.bps[p]) > n:
                return True
        return False

    def select_candidate_bps(self, n=30, m=10):
        if n is None:
            n = 30
        if m is None:
            m = 5
        candidate_bps = {}
        for bp in self.bps:
            if len(self.bps[bp]) >= m and len(self.bps[bp]) <= n and self.has_parent_with_direct_annotations_greater_than(bp, n):
                candidate_bps[bp] = self.bps[bp]
        return candidate_bps

    def term_subset(self, term_list):
        new_list = {}
        for t in term_list:
            new_list[t] = self.bps[t]
        return new_list

    def get_our_nice_lists(self, n=30, m=10):
        self.candidate_bps = self.select_candidate_bps(n, m)
        self.pair_list = self.grouper.pair_bp_sets_with_similar_genes(m)
        self.cluster_list = self.grouper.cluster_pair_list(self.pair_list)
        self.unpaired_bps = self.grouper.find_unpaired_bps(self.pair_list)

class BPTermSimilarityGrouper():
    def __init__(self, tad):
        self.tad = tad
        self.analyzer = GOTermAnalyzer(tad.ontology)
        
    def cluster_bps_with_similar_genes(self, m=10):
        set_list = []
        bp_dict = self.tad.bps
        bp_count = len(bp_dict)
        progress = ProgressTracker(bp_count)
        bpx_term, bpx_genes = bp_dict.popitem()
        while bpx_term is not None:
            for bpy in bp_dict:
                if bpx_term != bpy and not self.already_clustered(bpx_term, bpy, set_list):
                    if len(set(bpx_genes) & set(bp_dict[bpy])) >= min(m, len(bpx_genes), len(bp_dict[bpy])):
                        self.add_to_clusters(bpx_term, bpy, set_list)
            progress.print_progress()
            if len(bp_dict) == 0:
                break
            else:
                bpx_term, bpx_genes = bp_dict.popitem()
        return set_list

    def already_clustered(self, term1, term2, bp_clusters):
        for cluster in bp_clusters:
            if term1 in cluster or term2 in cluster:
                return True
        return False

    def add_to_clusters(self, term1, term2, bp_clusters):
        c_index = 0
        for cluster in bp_clusters:
            if term1 in cluster:
                bp_clusters[c_index].append(term2)
            elif term2 in cluster:
                bp_clusters[c_index].append(term1)
            else:
                bp_clusters.append([term1, term2])
            c_index += 1
        if len(bp_clusters) == 0:
            bp_clusters.append([term1, term2])

    def pair_bp_sets_with_similar_genes(self, bp_dict, m=5):
        if m is None:
            m = 5
        set_list = []
        progress = ProgressTracker(len(bp_dict), "pairing BP terms by gene set similarity")
        for bpx in bp_dict:
            for bpy in bp_dict:
                if bpx != bpy and (bpx, bpy) not in set_list:
                    if len(set(bp_dict[bpx]) & set(bp_dict[bpy])) >= min(m, len(bp_dict[bpx]), len(bp_dict[bpy])):
                        set_list.append((bpx, bpy))
            progress.print_progress()
        return set_list

    def cluster_pair_list(self, result_sets):
        clusters = []
        for r_set in result_sets:
            c_index = 0
            cluster_found = False
            term1 = r_set[0]
            term2 = r_set[1]
            for cluster in clusters:
                if term1 in cluster and term2 in cluster:
                    cluster_found = True
                    break
                elif term1 in cluster:
                    if term2 not in cluster:
                        clusters[c_index].append(term2)
                        cluster_found = True
                        break
                elif term2 in cluster:
                    if term1 not in cluster:
                        clusters[c_index].append(term1)
                        cluster_found = True
                        break
                c_index += 1
            if not cluster_found:
                clusters.append([term1, term2])
        return clusters

    def uniqueify_tuple_terms(self, tuples):
        unique_items = [] 
        for a, b in tuples:
            if a not in unique_items:
                unique_items.append(a)
            if b not in unique_items:
                unique_items.append(b)
        return unique_items

    def find_unpaired_bps(self, plist):
        bp_dict = self.tad.bps
        unpaired_bp_list = []
        unique_bps = self.uniqueify_tuple_terms(plist)
        for bp in bp_dict:
            if bp not in unique_bps:
                unpaired_bp_list.append(bp)
        return unpaired_bp_list

    def print_clusters(self, clusters, c_out=None):
        cluster_counter = 1
        if c_out is None:
            for c in clusters:
                print("---- Cluster " + str(cluster_counter) + ", Total: " + str(len(c)) + " ----")
                for t in c:
                    print(t + " - " + self.analyzer.label(t))
                cluster_counter += 1
        else:
            with open(c_out, 'w') as f:
                for c in clusters:
                    f.write("---- Cluster " + str(cluster_counter) + ", Total: " + str(len(c)) + " ----\n")
                    for t in c:
                        f.write(t + " - " + self.analyzer.label(t) + "\n")
                    cluster_counter += 1

class ProgressTracker():
    def __init__(self, total, title=None):
        self.start = datetime.datetime.now()
        self.progress_counter = 0
        self.current_progress = None
        self.total = total
        self.title = title

    def print_progress(self):
        self.progress_counter += 1
        updated_progress = int(math.floor((self.progress_counter / self.total) * 100))
        if self.current_progress is None or updated_progress > self.current_progress:
            self.current_progress = updated_progress
            if self.title is not None:
                print(str(self.current_progress) + "% complete - " + self.title)
            else:
                print(str(self.current_progress) + "% complete")
            if self.current_progress != 100:
                sys.stdout.write("\033[F")

    def execution_time(self):
        return datetime.datetime.now() - self.start

def do_everything(n=30, m=10, outfile=None, c_out=None, s_out=None, gaf_file=None):
    # onto, aset = setup_pombase()
    ontology = OntologyFactory().create("go")
    afactory = AssociationSetFactory()
    association_set = afactory.create(ontology, "gene", "function", taxon=POMBASE)
    tad = TermAnnotationDictionary(ontology, association_set)
    tad.print_results(outfile)
    print("Initial BP count: " + str(len(tad.bps)))
    new_bps = tad.select_candidate_bps(n, m)
    print("Candidate BP count: " + str(len(new_bps)))
    grouper = BPTermSimilarityGrouper(tad)
    p_list = grouper.pair_bp_sets_with_similar_genes(new_bps, m)
    print("BP pair count: " + str(len(p_list)))
    c_list = grouper.cluster_pair_list(p_list)
    unpaired_bps = grouper.find_unpaired_bps(p_list)
    tad.print_results(s_out, tad.term_subset(unpaired_bps))
    grouper.print_clusters(c_list, c_out)

def process_tad_and_dump_out(filename, json_outfile):
    ontology = OntologyFactory().create("go")
    afactory = AssociationSetFactory()
    a_set = afactory.create(ontology, file=filename, fmt="gaf")
    tad = TermAnnotationDictionary(ontology, a_set)
    tad.dump_to_json(json_outfile)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-t', "--term_gene_count_outfile", type=str, required=False,
                        help="File name of BP term listing count of gene sets")
    parser.add_argument('-c', "--clusters_outfile", type=str, required=False,
                        help="File name of BP term clusters list")
    parser.add_argument('-s', "--unclustered_outfile", type=str, required=False,
                        help="File name of BP terms that did not cluster (singletons)")
    parser.add_argument('-n', "--n_value", type=str, required=False,
                        help="1. Get all the BP terms X, where the number of genes annotated to X are less than or equal to n, and the number of genes annotated to a (is_a or part_of) parent of X are greater than n")
    parser.add_argument('-m', "--m_value", type=str, required=False,
                        help="2. Of the BPs from step 1, cluster BPs X and Y together if the number of genes in common between the a pair is greater than or equal to min(m, number of genes in X, number of genes in Y)")
    parser.add_argument('-g', "--gaf_source", type=str, required=True,
                        help="filename of GAF file to use as annotation source")
    parser.add_argument('-j', "--dump_tad_json", type=str, required=False,
                        help="Save TermAnnotationDictionary values to json file for reuse")

    args = parser.parse_args()
    if args.n_value is not None:
        args.n_value = int(args.n_value)
    if args.m_value is not None:
        args.m_value = int(args.m_value)

    if args.dump_tad_json is not None:
        print("Saving TermAnnotationDictionary values to " + args.dump_tad_json + "...")
        process_tad_and_dump_out(args.gaf_source, args.dump_tad_json)
        print("JSON created")
    else:
        print("Getting your lists for you...")
        do_everything(args.n_value, args.m_value, args.term_gene_count_outfile, args.clusters_outfile, args.unclustered_outfile)

if __name__ == "__main__":
    main()