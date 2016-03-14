# -*- coding: utf-8 -*-

from sollya import S2, ceil, log2, abs

def ulp(v, format_):
    """ return a 'unit in last place' value for <v> assuming precision is defined by format _ """
    return S2**(ceil(log2(abs(v))) - (format_.get_precision() + 1))

