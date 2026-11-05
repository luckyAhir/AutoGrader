#include <stdio.h>
#include <stdlib.h>

/* compare function for qsort */
int cmp(const void *a, const void *b)
{
    return *(const int *)a - *(const int *)b;
}

int main(void)
{
    int n;
    if (scanf("%d", &n) != 1) return 1;
    int *arr = malloc(sizeof(int) * (n + 1));
    for (int i = 0; i < n; i++) scanf("%d", &arr[i]);
    qsort(arr, n, sizeof(int), cmp);
    for (int i = 0; i < n; i++) {
        if (i > 0) printf(" ");
        printf("%d", arr[i]);
    }
    printf("\n");
    free(arr);
    return 0;
}
