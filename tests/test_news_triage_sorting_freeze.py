"""Offline recording and independently completed human evidence lists."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from scripts.freeze_news_triage_sorting import CASES, preview_directory, main
from trading_companion.daily_bar import DailyBar
from trading_companion.errors import DataSourceError
from trading_companion.news_triage_sorting_annotations import (
    evidence, load_seal, seal_annotations, ANNOTATION,
)
from trading_companion.news_triage_sorting_artifacts import canonical, read_json, write_json
from trading_companion.news_triage_sorting_freeze import (
    CaseSpec, SourceTransport, RecordingSources, collection_plan, freeze_batch, rebuild_case, prepare_case,
)
from trading_companion.news_triage_sorting_replay import (
    DailyPacket, ReplayCase, VERSION, freeze_case, load_manifest, encode, decode,
)
from trading_companion.multi_analysis import bind_retrieval_clock
from trading_companion.product_research import collect_product_research, bind_research_request_ids
from tests.test_product_research import NOW, CompanyDataFixture


class DailyFactory:
    def __init__(self):self.reads=[]
    def __call__(self, request):
        owner=self
        class Source:
            source_name=request.source
            adjustment="unadjusted"
            instrument_id=request.instrument_id
            start_date=request.start_date
            end_date=request.as_of_date
            def read_daily_bars(self):
                owner.reads.append(request)
                dates=[request.start_date+timedelta(days=i) for i in range((request.as_of_date-request.start_date).days+1)]
                return [DailyBar(request.instrument_id,d,10+i/1000,11+i/1000,9+i/1000,10+i/1000,
                                 100000+i,1000000.0) for i,d in enumerate(dates) if d.weekday()<5]
        return Source()


def frozen(spec=CASES[0], recorder=None):
    directory=preview_directory(NOW)
    recorder=recorder or RecordingSources(CompanyDataFixture(), DailyFactory(), clock=lambda:NOW)
    recorder.batches,recorder.daily_packets=[],[]
    question,checked,plan=prepare_case(spec,directory,NOW)
    with bind_retrieval_clock(lambda:recorder.retrieved_at), bind_research_request_ids(
            "sorting-"+spec.id+"-daily-"+str(i) for i in range(len(plan.securities))):
        bundle=collect_product_research(plan,data_service=recorder,source_factory=recorder.daily,now=NOW)
    return ReplayCase(spec.id,question,checked,NOW,directory,tuple(recorder.batches),tuple(recorder.daily_packets),
        expected_plan=canonical(plan.to_dict()),expected_bundle=canonical(bundle.to_dict()),
        case_group=spec.group,selection_reason=spec.reason)


def manifest_files(root, *, mixed=False):
    case=frozen() if mixed else frozen(CASES[8])
    rows=[]
    for i in range(30):
        c=replace(case,id="case"+str(i))
        packet=root/(c.id+".json")
        rows.append(dict(file=packet.name,sha256=freeze_case(c,packet),first="on" if i<15 else "off"))
    manifest=root/"manifest.json"
    digest=write_json(manifest,dict(version=VERSION,cases=rows))
    cases,_=load_manifest(manifest)
    files=[]
    for who in ("codex","claude"):
        labels=[]
        for (c,_),row in zip(cases,rows,strict=True):
            e=next(k for k,v in evidence(c).items() if v["kind"]=="news")
            labels.append(dict(id=c.id,packet_sha256=row["sha256"],completed=True,none_reason="",
                               items=[dict(description="人工重要事项",evidence_ids=[e])]))
        path=root/(who+".json")
        write_json(path,dict(version=ANNOTATION,annotator=who,manifest_sha256=digest,cases=labels))
        files.append(path)
    return manifest,digest,*files


class FreezeTests(unittest.TestCase):
    def setUp(self):
        for guard in (patch("socket.socket.connect",side_effect=AssertionError("offline")),
                      patch("trading_companion.local_env._read_local_values",side_effect=AssertionError("no env"))):
            guard.start();self.addCleanup(guard.stop)

    def test_default_preview_exact_approved_composition_and_256_public_bound(self):
        plan=collection_plan(CASES,preview_directory(NOW),NOW)
        self.assertEqual((plan["company_http_upper_bound"],plan["daily_http_upper_bound"],plan["source_http_upper_bound"]),(172,84,256))
        self.assertEqual(len(CASES),30)
        self.assertEqual(sum(c.group=="decision" for c in CASES),6)
        self.assertEqual(sum(c.group.startswith("capacity") for c in CASES),4)
        self.assertEqual(sum(row["first"]=="on" for row in plan["cases"]),15)
        for spec in CASES[-4:]:
            _,_,p=prepare_case(spec,preview_directory(NOW),NOW)
            self.assertEqual({i.kind for i in p.indicators},{"sma","atr","macd","rsi","boll"})
            self.assertIn("日线与周线",p.user_question)
        with patch("sys.stdout",new=StringIO()) as output:
            self.assertEqual(main([]),0)
            self.assertEqual(json.loads(output.getvalue())["model_requests"],0)

    def test_complete_financial_technical_daily_weekly_decision_packets_rebuild_identically(self):
        for spec in (CASES[0],CASES[12],CASES[26],CASES[28]):
            with self.subTest(spec=spec.id):
                case=frozen(spec)
                self.assertEqual(canonical(rebuild_case(decode(encode(case))).to_dict()),case.expected_bundle)
                self.assertTrue(all(type(p) is DailyPacket and p.retrieved_at==NOW for p in case.daily))
                self.assertFalse(any(s["status"] in ("partial","unavailable")
                    for c in case.expected_bundle["companies"] for s in c["coverage"]))
                if spec.group.startswith("capacity"):
                    self.assertIn("completed_week_derived",{e["temporal_scope"] for e in evidence(case).values()})
        wrong=replace(case,expected_bundle={**case.expected_bundle,"evidence_count":-1})
        with self.assertRaises(ValueError):rebuild_case(wrong)

    def test_exact_cache_includes_failed_data_and_preserves_case_daily_ids(self):
        data,factory=CompanyDataFixture(),DailyFactory()
        recorder=RecordingSources(data,factory,clock=lambda:NOW)
        first=frozen(CASES[0],recorder)
        second=frozen(replace(CASES[0],id="second"),recorder)
        self.assertEqual(len(data.calls),1)
        self.assertEqual(len(factory.reads),1)
        self.assertNotEqual(first.daily[0].request.request_id,second.daily[0].request.request_id)
        rebuild_case(second)
        failing=Mock();failing.collect.side_effect=DataSourceError("private")
        recorder=RecordingSources(failing,factory)
        req=first.batches[0].request
        for _ in range(2):
            with self.assertRaises(DataSourceError):recorder.collect(req,("news",))
        self.assertEqual(failing.collect.call_count,1)

    def test_source_transport_counts_failures_before_http_and_enforces_endpoints_body_limit(self):
        url="https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
        client=Mock();client.request.side_effect=OSError("offline")
        t=SourceTransport(client,1)
        with self.assertRaises(OSError):t.get(url)
        with self.assertRaises(DataSourceError):t.get(url)
        self.assertEqual((t.used,client.request.call_count),(1,1))
        with self.assertRaises(ValueError):t.get("https://example.invalid")
        for n in (0,257,True):
            with self.assertRaises(ValueError):SourceTransport(client,n)
        response=Mock(status_code=200)
        response.iter_content.return_value=iter([b'{"ok":true}'])
        client.request.side_effect=None;client.request.return_value=response
        t=SourceTransport(client,2,sleeper=Mock())
        self.assertEqual(t.get(url).json(),{"ok":True})
        self.assertTrue(client.request.call_args.kwargs["stream"])
        self.assertFalse(client.request.call_args.kwargs["allow_redirects"])
        response.iter_content.return_value=iter([b"x"*512001])
        with self.assertRaises(DataSourceError):t.get(url)
        self.assertEqual(t.used,2)
        self.assertEqual(response.close.call_count,2)

    def test_buffered_transport_preserves_sina_utf8_and_gbk_decoding(self):
        from tests.test_company_news_sina import html, NOW as stamp, CUTOFF
        from trading_companion.company_news_sina import SinaCompanyNewsAdapter
        from trading_companion.company_research_contracts import ResearchDataRequest
        for encoding in ("utf-8","gb18030"):
            body=html().encode(encoding)
            response=Mock(status_code=200,headers={"Content-Type":"text/html; charset="+encoding})
            response.iter_content.return_value=[body]
            client=Mock();client.request.return_value=response
            transport=SourceTransport(client,1)
            section=SinaCompanyNewsAdapter(session=transport,clock=lambda:stamp).fetch_section(
                ResearchDataRequest("300308.SZ","中际旭创",CUTOFF))
            self.assertEqual(section.status,"available")
            self.assertEqual(section.items[0].value,"中际旭创人工新闻标题")

    def test_partial_collection_preserves_packets_checkpoints_and_no_manifest(self):
        with TemporaryDirectory() as temp:
            root=Path(temp)/"frozen"
            report=freeze_batch(CASES,preview_directory(NOW),NOW,root,
                recorder=RecordingSources(CompanyDataFixture(unavailable=("news",)),DailyFactory(),clock=lambda:NOW))
            self.assertEqual(report["status"],"partial")
            self.assertFalse((root/"manifest.json").exists())
            self.assertTrue((root/"case01.json").exists())
            self.assertEqual(len(list(root.glob("collection-progress-*.json"))),30)
            with self.assertRaises(FileExistsError):
                freeze_batch(CASES,preview_directory(NOW),NOW,root,recorder=None)

    def test_complete_collection_writes_v2_manifest_and_uncompleted_independent_templates(self):
        with TemporaryDirectory() as temp:
            root=Path(temp)/"frozen"
            report=freeze_batch(CASES,preview_directory(NOW),NOW,root,
                recorder=RecordingSources(CompanyDataFixture(),DailyFactory(),clock=lambda:NOW))
            self.assertEqual(report["status"],"completed")
            cases,plan=load_manifest(root/"manifest.json")
            self.assertEqual(plan["version"],VERSION)
            self.assertEqual([c.id for c,_ in cases],[s.id for s in CASES])
            for who in ("codex","claude"):
                labels,_=read_json(root/("annotations-"+who+".template.json"))
                self.assertEqual(labels["annotator"],who)
                self.assertFalse(any(row["completed"] for row in labels["cases"]))

    def test_interruption_persists_partial_collection_report(self):
        with TemporaryDirectory() as temp:
            root=Path(temp)/"frozen"
            service=Mock();service.collect.side_effect=KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                freeze_batch(CASES,preview_directory(NOW),NOW,root,
                    recorder=RecordingSources(service,DailyFactory(),clock=lambda:NOW))
            report,_=read_json(root/"collection-report.json")
            self.assertEqual(report["status"],"partial")
            self.assertEqual(report["cases"][0]["reason"],"collection_interrupted")
            self.assertFalse((root/"manifest.json").exists())


class AnnotationTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.manifest,self.digest,self.codex,self.claude=manifest_files(self.root)

    def change(self,path,mutate):
        data,_=read_json(path);mutate(data)
        path.write_text(json.dumps(data,ensure_ascii=False),encoding="utf-8")

    def test_complete_none_requires_explicit_reason_and_unreviewed_or_placeholders_rejected(self):
        original,_=read_json(self.codex)
        for change in (dict(completed=False),dict(items=[],none_reason=""),dict(none_reason="TODO"),
                       dict(items=[dict(description="x",evidence_ids=["E999"])]) ):
            data=deepcopy(original);data["cases"][0].update(change)
            self.codex.write_text(json.dumps(data),encoding="utf-8")
            with self.assertRaises(ValueError):seal_annotations(self.manifest,self.codex,self.claude)
        for path in (self.codex,self.claude):
            data=deepcopy(original);data["annotator"]="codex" if path==self.codex else "claude"
            data["cases"][0].update(items=[],none_reason="已逐项审核，没有重要相关事项")
            path.write_text(json.dumps(data),encoding="utf-8")
        seal_annotations(self.manifest,self.codex,self.claude)
        items,_=load_seal(self.manifest,self.codex,self.claude)
        self.assertEqual(items["case0"],[])
        self.change(self.codex,lambda d:d["cases"][1].update(none_reason="changed"))
        with self.assertRaises(ValueError):load_seal(self.manifest,self.codex,self.claude)

    def test_disagreement_requires_bound_claude_adjudication_and_retains_original(self):
        self.change(self.claude,lambda d:d["cases"][0]["items"][0].update(description="另一独立判断"))
        with self.assertRaisesRegex(ValueError,"adjudication"):seal_annotations(self.manifest,self.codex,self.claude)
        left,lh=read_json(self.codex);right,rh=read_json(self.claude)
        row={**right["cases"][0],"reason":"Claude依据原E证据逐项裁定，保留独立输入"}
        path=self.root/"adjudication.json"
        write_json(path,dict(version="sorting-adjudication-v1",annotator="claude",manifest_sha256=self.digest,
                             codex_sha256=lh,claude_sha256=rh,cases=[row]))
        seal_annotations(self.manifest,self.codex,self.claude,adjudication=path)
        self.assertEqual(load_seal(self.manifest,self.codex,self.claude)[0]["case0"][0]["description"],"另一独立判断")
        self.change(self.root/"annotation-adjudication.json",lambda d:d["cases"][0].update(reason="改动"))
        with self.assertRaises(ValueError):load_seal(self.manifest,self.codex,self.claude)

    def test_cross_company_important_item_cannot_merge_two_issuers_evidence(self):
        from trading_companion.news_triage_sorting_annotations import _label
        case=frozen(CASES[28])
        refs=[key for key,value in evidence(case).items() if value["kind"]=="news"]
        row=dict(id=case.id,packet_sha256="a"*64,completed=True,none_reason="",
                 items=[dict(description="不可挪用另一公司事实",evidence_ids=refs[:2])])
        with self.assertRaisesRegex(ValueError,"one company"):_label(row,case,"a"*64)


if __name__=="__main__":unittest.main()
