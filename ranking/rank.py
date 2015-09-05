"""
Rank and Filter support for ctags plugin for Sublime Text 2/3.
"""

import functools
from functools import reduce
import locale
import sys
import os
import pprint
import re
import string
import threading
import subprocess
from itertools import chain
from operator import itemgetter as iget
from collections import defaultdict, deque

from helpers.common import *

def compile_definition_filters(view):
    filters = []
    for selector, regexes in list(get_setting('definition_filters', {}).items()):
        if view.match_selector(view.sel() and view.sel()[0].begin() or 0,
                               selector):
            filters.append(regexes)
    return filters

def get_grams(str):
    lstr = str.lower()
    return set(zip(lstr,lstr[1:],lstr[2:]))

class RankMgr:
    """
    For each matched Tag, calculates the rank score or filter it out. The remaining matches are sorted by decending score. 
    """
    def __init__(self,region, mbrParts, view):
        print('RankMgr init')
        
        self.region = region
        self.mbrParts = mbrParts
        self.view = view

        self.def_filters = compile_definition_filters(view)

        self.fname_abs = view.file_name().lower() if not(view.file_name() is None) else None        
        
         # Return a set of tri-grams (each tri-gram is a tuple) given a string: 
        # Ex: 'Dekel' --> {('d', 'e', 'k'), ('k', 'e', 'l'), ('e', 'k', 'e')}
        
        mbrGrams = [get_grams(part) for part in mbrParts];
        self.setMbrGrams = (reduce(lambda s,t: s.union(t), mbrGrams) if mbrGrams else set() )
        print('setMbrGrams = %s' % self.setMbrGrams);
            
    # Object Member Expression File Ranking: Rank higher candiates tags path names that fuzzy match the <expression>.method()
    # Rules:
    # 1) youtube.fetch() --> mbrPaths = ['youtube'] --> get_rank of tag 'fetch' with rel_path a/b/Youtube.js ---> RANK_EXACT_MATCH_RIGHTMOST_MBR_PART_TO_FILENAME
    # 2) youtube.fetch() --> user GotoDef from youtube.js --> RANK_EQ_FILENAME_RANK
    # 3) vidtube.fetch() --> tag 'fetch' with rel_path google/video/youtube.js ---> fuzzy match of tri-grams of vidtube (vid,idt,dtu,tub,ube) with tri-grams from the path
    RANK_EQ_FILENAME_RANK = 10
    RANK_EXACT_MATCH_RIGHTMOST_MBR_PART_TO_FILENAME = 20
    WEIGHT_RIGHTMOST_MBR_PART = 2
    MAX_WEIGHT_GRAM = 3
    WEIGHT_DECAY = 1.5
    reThis = re.compile('this|self|me|that', re.IGNORECASE) #TODO: this/self config
    def get_rank(self,rel_path,mbrParts):
#           print('get_rank.rel_path = %s' % rel_path);
        
        rank = 0
        rel_path_no_ext = rel_path.lstrip('.' + os.sep)
        rel_path_no_ext = os.path.splitext(rel_path_no_ext)[0]
        pathParts = rel_path_no_ext.split(os.sep);
        if len(pathParts) >= 1 and len(mbrParts) >= 1 and pathParts[-1].lower() == mbrParts[-1].lower():
            rank += self.RANK_EXACT_MATCH_RIGHTMOST_MBR_PART_TO_FILENAME
#                print('Boost: pathParts[-1].lower() == mbrParts[-1].lower() %d' % rank)
                
        # Same file --> Boost rank
        if self.eq_filename(rel_path): 
            rank += self.RANK_EQ_FILENAME_RANK
            print('Same file: %d' % rank)
            if len(mbrParts) == 1 and self.reThis.match(mbrParts[-1]):
                rank += self.RANK_EQ_FILENAME_RANK # this.mtd() -  rank candidate from current file very high.
                print('Same file + this: %d' % rank)
                return rank
            
        # Prepare dict of <tri-gram : weight>, where weight decays are we move further away from the method call (to the left)
        pathGrams = [get_grams(part) for part in pathParts];
#           print('pathGrams = %s' % pathGrams);
        wt = self.MAX_WEIGHT_GRAM
        dctPathGram = {}
        for setPathGram in reversed(pathGrams):                
            dctPathPart = dict.fromkeys(setPathGram,wt)
            dctPathGram = merge_two_dicts_shallow(dctPathPart,dctPathGram)
            wt /= self.WEIGHT_DECAY
        
#            print('dctPathGram = %s' % dctPathGram);
        
        for mbrGrm in self.setMbrGrams:
            rank += dctPathGram.get(mbrGrm,0)
        
#           print('rank = %d' % rank);
        return rank

    def pass_def_filter(self,o):
            for f in self.def_filters:
                for k, v in list(f.items()):
                    if k in o:
                        if re.match(v, o[k]):
                            return False
            return True
        
        
    def eq_filename(self,rel_path):
        if self.fname_abs is None or rel_path is None:
            return False    
        return self.fname_abs.endswith(rel_path.lstrip('.').lower())
    
    # Given optional scope extended field tag.scope = 'startline:startcol-endline:endcol' -  def-scope. 
    # Return: Tuple of 2 Lists: 
    #  in_scope: Tags with matching scope: current cursor / caret position is contained in their start-end scope range.
    #  no_scope: Tags without scope or with global scope 
    # Usage: locals, local parameters Tags have scope (ex: in estr.js tag generator for JavaScript)
    def scope_filter(self,taglist):
        in_scope = []
        no_scope = []
        for tag in taglist:
            if self.region is None or tag.get('scope') is None or tag.scope is None or tag.scope == 'global':
                no_scope.append(tag)
                continue

            if not self.eq_filename(tag.filename):
                continue
        
            mch = re.search(get_setting('scope_re'),tag.scope) 
            
            if mch:
                beginLine = int(mch.group(1)) - 1 # .tags file is 1 based and region.begin() is 0 based
                beginCol  = int(mch.group(2)) - 1
                endLine = int(mch.group(3)) - 1
                endCol  = int(mch.group(4)) - 1
                beginPoint = self.view.text_point(beginLine,beginCol)
                endPoint = self.view.text_point(endLine,endCol)                
                if self.region.begin() >= beginPoint and self.region.end() <= endPoint:                    
                    in_scope.append(tag)
                    

        return (in_scope,no_scope)    

    def sort_tags(self,taglist):
        # Scope Filter: If symbol matches at least 1 local scope tag - assume they hides non-scope and global scope tags. 
        # If no local-scope (in_scope) matches --> keep the global / no scope matches (see in sorted_tags) and discard 
        # the local-scope - because they are not locals of the current position                                
        # If object-receiver (someobj.symbol) --> refer to as global tag --> filter out local-scope tags
        (in_scope,no_scope) = self.scope_filter(taglist)
        if (len(self.setMbrGrams) == 0 and len(in_scope) > 0): #TODO:Config: @symbol - in Ruby instance var (therefore never local var)
            p_tags = in_scope
        else:                
            p_tags = no_scope

        p_tags = list(filter(lambda tag: self.pass_def_filter(tag), p_tags))
        p_tags = sorted(p_tags, key=lambda tag: self.get_rank(tag.tag_path[0],self.mbrParts),reverse=True )   
        return p_tags

