from decimal import Decimal
import pytest
from p4_designation import normalise_type_code,parse_thickness,clean_rows,attributes_from_description,parse_page

def row(code='Fe//Zn12/Cn//T2',thickness='12'):
    return dict(typ=code,beschichtung='Zink Dickschicht passiviert und Versiegelung',schichtdicke=thickness,trommel='Ja',gestell='Nein')

def test_slash_repair_keeps_raw():
    result=clean_rows([row()])[0]
    assert result['type_code_raw']=='Fe//Zn12/Cn//T2'
    assert result['type_code']=='Fe//Zn12//Cn//T2'
    assert result['quality_flags']==['slash_fixed']

@pytest.mark.parametrize('raw,expected',[('8 - 25',(8,25)),('6–10',(6,10)),('8,5 µm',(Decimal('8.5'),None)),('',(None,None))])
def test_thickness(raw,expected):assert parse_thickness(raw)==expected

@pytest.mark.parametrize('raw',['25 - 8','8 or 12','-5','8 / 12 / 25'])
def test_unknown_thickness_not_guessed(raw):
    with pytest.raises(ValueError):parse_thickness(raw)

def test_ambiguous_codes_preserved():
    rows=clean_rows([row('Ofl-r645','8 - 25'),row('Ofl-r645','8 - 15')],'VW 13750 TL244')
    assert len(rows)==2 and rows[0]['row_no']!=rows[1]['row_no']
    assert all('type_code_not_unique' in r['quality_flags'] for r in rows)

def test_non_iso_single_value_is_unconfirmed():
    result=clean_rows([row('ABC','12')],'Other norm')[0]
    assert result['thickness_min_um'] is None
    assert 'single_thickness_semantics_unconfirmed' in result['quality_flags']

def test_optional_seal_and_phosphate_not_misclassified():
    assert attributes_from_description('Zink Versiegelung wahlfrei')['sealed'] is None
    assert attributes_from_description('Zink-Phosphat')['coating_system'] is None

def test_ragged_markup_rejected():
    html='<h1>Example</h1><table><tr>'+''.join('<th>'+h+'</th>' for h in ['Typ','Beschichtung','Schichtdicke','Trommel','Gestell'])+'</tr><tr><td>missing cells</td></tr></table>'
    with pytest.raises(ValueError):parse_page(html)
