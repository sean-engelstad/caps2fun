import utils
import os

gif_writer = utils.GifWriter(frames_per_second=10)
gif_writer(gif_filename="deformWing2.gif", path=os.getcwd())
