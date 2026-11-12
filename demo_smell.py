import os


def risky(a):
    unused = 1
    try:
        x = int(a)
    except:
        pass
    return x == x
