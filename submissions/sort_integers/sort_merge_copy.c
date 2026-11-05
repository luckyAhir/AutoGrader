#include <stdio.h>
#include <stdlib.h>

static void out(const int *arr, size_t len)
{
    for (size_t p = 0; p < len; p++) {
        printf(p == 0 ? "%d" : " %d", arr[p]);
    }
    printf("\n");
}

static int *input_array(size_t len)
{
    int *arr = malloc((len > 0 ? len : 1) * sizeof *arr);

    if (arr == NULL) {
        return NULL;
    }
    for (size_t p = 0; p < len; p++) {
        if (scanf("%d", &arr[p]) != 1) {
            free(arr);
            return NULL;
        }
    }
    return arr;
}

static void combine(int *arr, int *tmp, size_t lo, size_t md, size_t hi)
{
    size_t a = lo, b = md, c = lo;

    while (a < md && b < hi) {
        if (arr[a] <= arr[b]) {
            tmp[c++] = arr[a++];
        } else {
            tmp[c++] = arr[b++];
        }
    }
    while (a < md) {
        tmp[c++] = arr[a++];
    }
    while (b < hi) {
        tmp[c++] = arr[b++];
    }
    for (c = lo; c < hi; c++) {
        arr[c] = tmp[c];
    }
}

static void msort(int *arr, int *tmp, size_t lo, size_t hi)
{
    if (hi - lo < 2) {
        return;
    }
    size_t md = lo + (hi - lo) / 2;
    msort(arr, tmp, lo, md);
    msort(arr, tmp, md, hi);
    combine(arr, tmp, lo, md, hi);
}

int main(void)
{
    int n;

    if (scanf("%d", &n) != 1 || n < 0) {
        fprintf(stderr, "bad n\n");
        return EXIT_FAILURE;
    }
    size_t len = (size_t)n;
    int *arr = input_array(len);
    int *tmp = malloc((len > 0 ? len : 1) * sizeof *tmp);
    if (arr == NULL || tmp == NULL) {
        free(arr);
        free(tmp);
        fprintf(stderr, "bad input\n");
        return EXIT_FAILURE;
    }
    msort(arr, tmp, 0, len);
    out(arr, len);
    free(tmp);
    free(arr);
    return EXIT_SUCCESS;
}
