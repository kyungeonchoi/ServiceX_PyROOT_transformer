# Copyright (c) 2019, IRIS-HEP
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# * Neither the name of the copyright holder nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
import json
import sys
import traceback

import awkward
import awkward1
import time

from servicex.transformer.servicex_adapter import ServiceXAdapter
from servicex.transformer.transformer_argument_parser import TransformerArgumentParser
from servicex.transformer.kafka_messaging import KafkaMessaging
from servicex.transformer.object_store_manager import ObjectStoreManager
from servicex.transformer.rabbit_mq_manager import RabbitMQManager
from servicex.transformer.uproot_events import UprootEvents
from servicex.transformer.uproot_transformer import UprootTransformer
from servicex.transformer.arrow_writer import ArrowWriter
import uproot
import os
import pyarrow.parquet as pq
import pandas as pd
import pyarrow as pa

from ROOT import TTree, TFile, TObject, gDirectory, TLorentzVector
from array import array
import math
from itertools import combinations, product

# How many bytes does an average awkward array cell take up. This is just
# a rule of thumb to calculate chunksize
avg_cell_size = 42

messaging = None
object_store = None


class ArrowIterator:
    def __init__(self, arrow, chunk_size, file_path):
        self.arrow = arrow
        self.chunk_size = chunk_size
        self.file_path = file_path
        self.attr_name_list = ["not available"]

    def arrow_table(self):
        yield self.arrow


# noinspection PyUnusedLocal
def callback(channel, method, properties, body):
    transform_request = json.loads(body)
    print('transform_request: ', transform_request)
    _request_id = transform_request['request-id']
    _file_path = transform_request['file-path']
    _file_id = transform_request['file-id']
    _server_endpoint = transform_request['service-endpoint']
    _tree_name = transform_request['tree-name']
    # _selection = transform_request['selection']
    # _chunks = transform_request['chunks']
    servicex = ServiceXAdapter(_server_endpoint)

    tick = time.time()
    try:
        # Do the transform
        servicex.post_status_update(file_id=_file_id,
                                    status_code="start",
                                    info="tree-name: "+_tree_name)

        root_file = _file_path.replace('/', ':')
        output_path = '/home/atlas/' + root_file
        # print(f'selection: {_selection}')
        # transform_single_file(_file_path, output_path+".parquet", servicex, tree_name=_tree_name)
        transform_single_file(_file_path, output_path, servicex, tree_name=_tree_name)

        tock = time.time()

        # if object_store:
        #     object_store.upload_file(_request_id, root_file+".root", output_path+".root")
        #     os.remove(output_path+".root")    
        if object_store:
            object_store.upload_file(_request_id, root_file, output_path)
            os.remove(output_path)    


        servicex.post_status_update(file_id=_file_id,
                                    status_code="complete",
                                    info="Success")

        servicex.put_file_complete(_file_path, _file_id, "success",
                                   num_messages=0,
                                   total_time=round(tock - tick, 2),
                                   total_events=0,
                                   total_bytes=0)

    except Exception as error:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        traceback.print_tb(exc_traceback, limit=20, file=sys.stdout)
        print(exc_value)

        transform_request['error'] = str(error)
        channel.basic_publish(exchange='transformation_failures',
                              routing_key=_request_id + '_errors',
                              body=json.dumps(transform_request))

        servicex.post_status_update(file_id=_file_id,
                                    status_code="failure",
                                    info="error: "+str(exc_value)[0:1024])

        servicex.put_file_complete(file_path=_file_path, file_id=_file_id,
                                   status='failure', num_messages=0, total_time=0,
                                   total_events=0, total_bytes=0)
    finally:
        channel.basic_ack(delivery_tag=method.delivery_tag)


def transform_single_file(file_path, output_path, servicex=None, tree_name='Events'):
    print("Transforming a single path: " + str(file_path))

    # try:
    f_in = TFile.Open(file_path)
    print(f'Input file:{f_in}')
    
    f_out = TFile(output_path,'recreate')
    print(f'Onput file:{f_out}')

    # Copy Tree with selection
    tree = f_in.Get('NOMINAL')
    tree_new = tree.CopyTree('n_jets >= 5')

    # Copy non TTree objects
    non_ttree_list = ['f_in.' + key.GetName() + '.Write()' for key in f_in.GetListOfKeys() if key.GetClassName() != 'TTree']
    for obj in non_ttree_list:
        exec(obj)

    # Add new branches
    HTjets_new = array('f',[0.])
    SumPtBjet_new = array('f',[0.])
    jjdrmin_new = array('f',[99.])
    mWbest_new = array('f',[-999e3])
    mWsubbest_new = array('f',[-999e3])
    mTopWbest_new = array('f',[-999e3])
    chi2_top_estimator = array('f',[-999e3])
    dR_ditau_mmc_top = array('f',[99.])
    dR_bb = array('f',[99.])

    m_Higgs = 125.0e3 # MeV
    m_W = 80.379e03
    m_t = 172.5e03
    sigma_t = 13.80e3 # MeV
    sigma_W = 7.40e3 # MeV

    new_br_1 = tree_new.Branch('HTjets_new',HTjets_new,'HTjets_new/F')
    new_br_2 = tree_new.Branch('SumPtBjet_new',SumPtBjet_new,'SumPtBjet_new/F')
    new_br_3 = tree_new.Branch('jjdrmin_new',jjdrmin_new,'jjdrmin_new/F')
    new_br_4 = tree_new.Branch('mWbest_new',mWbest_new,'mWbest_new/F')
    new_br_5 = tree_new.Branch('mWsubbest_new',mWsubbest_new,'mWsubbest_new/F')
    new_br_6 = tree_new.Branch('mTopWbest_new',mTopWbest_new,'mTopWbest_new/F')
    new_br_7 = tree_new.Branch('chi2_top_estimator',chi2_top_estimator,'chi2_top_estimator/F')
    new_br_8 = tree_new.Branch('dR_ditau_mmc_top',dR_ditau_mmc_top,'dR_ditau_mmc_top/F')
    new_br_9 = tree_new.Branch('dR_bb',dR_bb,'dR_bb/F')

    entries = tree_new.GetEntriesFast()
    print(f'entries in tree_new:{entries}')
    for entry in range(entries):
        tree_new.GetEntry(entry)

    #     # Event loop

    #     # Jet lists
        jet_list_all = [tree_new.jet_0_p4, tree_new.jet_1_p4, tree_new.jet_2_p4, tree_new.jet_3_p4, tree_new.jet_4_p4, tree_new.jet_5_p4, tree_new.jet_6_p4, tree_new.jet_7_p4]
        jet_list = jet_list_all[slice(tree_new.n_jets)]
        # print(f'length after cut: {len(jet_list)}')

        btag_list_all = [ tree_new.jet_0_b_tagged_DL1r_FixedCutBEff_70, tree_new.jet_1_b_tagged_DL1r_FixedCutBEff_70, tree_new.jet_2_b_tagged_DL1r_FixedCutBEff_70, tree_new.jet_3_b_tagged_DL1r_FixedCutBEff_70, tree_new.jet_4_b_tagged_DL1r_FixedCutBEff_70, tree_new.jet_5_b_tagged_DL1r_FixedCutBEff_70, tree_new.jet_6_b_tagged_DL1r_FixedCutBEff_70, tree_new.jet_7_b_tagged_DL1r_FixedCutBEff_70 ]
        btag_list = btag_list_all[slice(tree_new.n_jets)]

        # jet_list = [eval(f'tree_new.jet_{i}_p4') for i in range(tree_new.n_jets) if i < 8]
    #     btag_list = [eval(f'tree_new.jet_{i}_b_tagged_DL1r_FixedCutBEff_70') for i in range(tree_new.n_jets) if i < 8]
        dijet_list = list(combinations(jet_list, 2))
        jjdrmin_list = [ i[0].DeltaR(i[1]) for i in dijet_list if i[0].DeltaR(i[1]) >= 1e-9 ]
        non_bjet_list = [ jet for jet, btag in zip(jet_list,btag_list) if btag==0]
        bjet_list = [ jet for jet, btag in zip(jet_list,btag_list) if btag==1]
        non_bjet_dijet_list = list(combinations(non_bjet_list, 2))
        mWcand_list = [(i[0] + i[1]).M()*1e3 for i in non_bjet_dijet_list]

        for mWcand, Wcand in zip(mWcand_list, non_bjet_dijet_list):
            if Wcand[0].DeltaR(Wcand[1]) >= 1e-9:
                if abs(mWcand - m_W) < abs(mWbest_new[0] - m_W):
                    if mWbest_new[0] > 0:
                        mWsubbest_new[0] = mWbest_new[0]
                    mWbest_new[0] = mWcand
                    mTopWbest_new[0] = -999e3

                    for bjet in bjet_list:
                        mTopcand = (Wcand[0] + Wcand[1] + bjet).M() * 1e3
                        if abs(mTopcand - m_t) < abs(mTopWbest_new[0] - m_t):
                            mTopWbest_new[0] = mTopcand


        HTjets_new[0] = math.fsum([jet.Pt()*1e3 for jet in jet_list])
        SumPtBjet_new[0] = math.fsum([jet.Pt()*1e3 for (jet,btag) in zip(jet_list,btag_list) if btag == 1])
        if len(jjdrmin_list) != 0:
            jjdrmin_new[0] = min(jjdrmin_list)
        else:
            jjdrmin_new[0] = 99.

        if len(bjet_list) >= 1 and len(non_bjet_list) >= 2:
            chi2_top_list = [((Wcand[0]+Wcand[1]+bjet).M()*1e3 - m_t)**2/(sigma_t*sigma_t) + ((Wcand[0]+Wcand[1]).M()*1e3 - m_W)**2/(sigma_W*sigma_W)  for bjet in bjet_list for Wcand in non_bjet_dijet_list]
            chi2_top_p4_list = [Wcand[0]+Wcand[1]+bjet for bjet in bjet_list for Wcand in non_bjet_dijet_list]
            chi2_top_estimator[0], index_min = min(chi2_top_list), min(range(len(chi2_top_list)), key=chi2_top_list.__getitem__)
            ditau_mmc = TLorentzVector()
            ditau_mmc.SetPtEtaPhiM(tree_new.ditau_mmc_maxw_pt, tree_new.ditau_mmc_maxw_eta, tree_new.ditau_mmc_maxw_phi, m_Higgs)
            # dR_ditau_top[0] = chi2_top_p4_list[index_min].DeltaR(tree_new.ditau_p4)
            dR_ditau_mmc_top[0] = chi2_top_p4_list[index_min].DeltaR(ditau_mmc)
            if len(bjet_list) == 2:
                dR_bb[0] = bjet_list[0].DeltaR(bjet_list[1])
        
        # Fill branches
        new_br_1.Fill()
        new_br_2.Fill()
        new_br_3.Fill()
        new_br_4.Fill()
        new_br_5.Fill()
        new_br_6.Fill()
        new_br_7.Fill()
        new_br_8.Fill()
        new_br_9.Fill()

        # Clear list
        mWbest_new[0] = -999e3
        mWsubbest_new[0] = -999e3
        mTopWbest_new[0] = -999e3
        chi2_top_estimator[0] = -999e3
        dR_ditau_mmc_top[0] = 99.
        dR_bb[0] = 99.        

    tree_new.Write("", TObject.kOverwrite)
    f_out.Close()



        # import generated_transformer
        # start_transform = time.time()
        # table = generated_transformer.run_query(file_path, tree_name)
        # end_transform = time.time()
        # print(f'generated_transformer.py: {round(end_transform - start_transform, 2)} sec')

        # start_serialization = time.time()        
        # table_awk1 = awkward1.from_awkward0(table)
        # new_table = awkward1.to_awkward0(table_awk1)
        # arrow = awkward.toarrow(new_table)
        # end_serialization = time.time()
        # print(f'awkward Table -> Arrow: {round(end_serialization - start_serialization, 2)} sec')

        # if output_path:
        #     writer = pq.ParquetWriter(output_path, arrow.schema)
        #     writer.write_table(table=arrow)
        #     writer.close()

    # except Exception:
    #     exc_type, exc_value, exc_traceback = sys.exc_info()
    #     traceback.print_tb(exc_traceback, limit=20, file=sys.stdout)
    #     print(exc_value)

    #     raise RuntimeError(
    #         "Failed to transform input file " + file_path + ": " + str(exc_value))

    # if messaging:
    #     arrow_writer = ArrowWriter(file_format=args.result_format,
    #                                object_store=None,
    #                                messaging=messaging)

    #     #Todo implement chunk size parameter
    #     transformer = ArrowIterator(arrow, chunk_size=1000, file_path=file_path)
    #     arrow_writer.write_branches_to_arrow(transformer=transformer, topic_name=args.request_id,
    #                                          file_id=None, request_id=args.request_id)


# def compile_code():
#     import generated_transformer
#     pass


if __name__ == "__main__":
    parser = TransformerArgumentParser(description="Uproot Transformer")
    args = parser.parse_args()

    print("-----", sys.path)
    kafka_brokers = TransformerArgumentParser.extract_kafka_brokers(args.brokerlist)

    print(args.result_destination, args.output_dir)
    if args.output_dir:
            messaging = None
            object_store = None
    elif args.result_destination == 'kafka':
        messaging = KafkaMessaging(kafka_brokers, args.max_message_size)
        object_store = None
    elif args.result_destination == 'object-store':
        messaging = None
        object_store = ObjectStoreManager()

    # compile_code()

    if args.request_id and not args.path:
        rabbitmq = RabbitMQManager(args.rabbit_uri, args.request_id, callback)

    if args.path:
        print("Transform a single file ", args.path)
        transform_single_file(args.path, args.output_dir)

# from transformer import transform_single_file
# transform_single_file('/data/group.phys-higgs.20856881._000001.HSM_common.root','output')
